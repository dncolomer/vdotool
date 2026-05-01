"""Tool handlers for the vdotool Hermes plugin.

Stdlib only (plus lazy imports into Hermes' own tool modules for
TTS/STT/vision). The active-session dict tracked in ``__init__.py`` is
passed in via the ``session_state`` keyword so handlers stay free of
module-level mutable state.

Filesystem contract:

    $VDOTOOL_FRAMES_DIR/<session_id>/
        frame-<unix_ms>.jpg
        latest.jpg                (symlink)
        audio_out/
            queue.json
            queue.json.lock
            muted_until_ms.txt
            <unix_ms>.mp3
        audio_in/
            utterance-<unix_ms>.webm
            .listener_status.json

The forked VDO.Ninja writes; this plugin reads.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import viewer as _viewer
from . import voice_config as _voice_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_FRAMES_DIR = "/var/lib/vdotool/frames"
DEFAULT_BASE_URL = "http://localhost:8080"
STALE_FRAME_SECONDS = 30
KEEP_FRAMES_AFTER_END_MIN = int(os.environ.get("VDOTOOL_KEEP_FRAMES_MIN", "10"))

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
ROOM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

# Blank-frame / low-detail heuristics — see plugin.yaml for details.
BLANK_BYTES_THRESHOLD = int(os.environ.get("VDOTOOL_BLANK_BYTES_THRESHOLD", "8000"))
LOW_DETAIL_BYTES_THRESHOLD = int(os.environ.get("VDOTOOL_LOW_DETAIL_BYTES_THRESHOLD", "15000"))

VISION_ANALYZE_ENABLED = os.environ.get("VDOTOOL_VISION_ANALYZE", "1") not in (
    "0", "false", "no", "off",
)
VISION_ANALYZE_TIMEOUT_SECONDS = float(os.environ.get("VDOTOOL_VISION_TIMEOUT", "60"))

STT_ENABLED = os.environ.get("VDOTOOL_STT_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off",
)

TTS_MAX_CHARS = int(os.environ.get("VDOTOOL_TTS_MAX_CHARS", "800"))
TTS_ENABLED = os.environ.get("VDOTOOL_TTS_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off",
)
TTS_MAX_CHARS_PER_MIN = int(os.environ.get("VDOTOOL_TTS_MAX_CHARS_PER_MIN", "10000"))

_STT_NOISE_BLACKLIST = {
    "", ".", "you", "you.", "thanks.", "thank you.", "thank you",
    "[music]", "[silence]", "♪", "bye", "bye.", "okay.", "ok.",
}


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _ok(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def _err(code: str, message: str, **extra: Any) -> str:
    payload: dict = {"error": {"code": code, "message": message}}
    if extra:
        payload["error"].update(extra)
    return json.dumps(payload)


def _frames_root() -> Path:
    return Path(os.environ.get("VDOTOOL_FRAMES_DIR", DEFAULT_FRAMES_DIR)).resolve()


def _base_url() -> str:
    return os.environ.get("VDOTOOL_VDO_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _session_dir(session_id: str) -> Path:
    root = _frames_root()
    target = (root / session_id).resolve()
    target.relative_to(root)  # raises ValueError on escape
    return target


def _latest_frame_path(session_state: dict) -> Path | None:
    sid = session_state.get("session_id")
    if not sid:
        return None
    session_dir = _session_dir(sid)
    if not session_dir.is_dir():
        return None
    latest = session_dir / "latest.jpg"
    if latest.exists() or latest.is_symlink():
        try:
            return latest.resolve(strict=True)
        except OSError:
            return None
    candidates = sorted(
        session_dir.glob("frame-*.jpg"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def latest_frame_age_seconds(session_state: dict) -> float | None:
    path = _latest_frame_path(session_state)
    if path is None:
        return None
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def _frame_timestamp_ms(path: Path) -> int | None:
    m = re.match(r"^frame-(\d+)\.jpg$", path.name)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def _build_links(base_url: str, room_id: str, session_id: str) -> tuple[str, str]:
    """Build VDO.Ninja push and view URLs for a vdocall.

    VDO.Ninja sanitizes stream IDs by replacing non-word characters
    with ``_`` on the push side; we use ``vt_<sid>`` (underscore, no
    hyphens) so both sides see the same stream id.
    """
    stream_id = f"vt_{session_id}"
    push = (
        f"{base_url}/?room={room_id}"
        f"&push={stream_id}"
        f"&webcam=1"
        f"&autostart=1"
        f"&quality=1"
        f"&cleanoutput=1"
    )
    view = (
        f"{base_url}/?room={room_id}"
        f"&view={stream_id}"
        f"&scene=1"
        f"&cleanoutput=1"
        f"&vdotool=1"
        f"&sessionId={session_id}"
    )
    return push, view


# ---------------------------------------------------------------------------
# Vision-analysis (optional) — auxiliary model describes the frame
# ---------------------------------------------------------------------------


def _vision_analyze_frame(path: Path, session_title: str | None, session_description: str | None) -> tuple[str | None, str | None]:
    if not VISION_ANALYZE_ENABLED:
        return None, "disabled"
    try:
        from tools.vision_tools import vision_analyze_tool  # type: ignore
    except Exception as e:  # noqa: BLE001
        return None, f"vision_analyze_tool unavailable: {e}"

    prompt_parts = [
        "You are watching a live video feed from a remote device. "
        "Describe ONLY what is actually visible in this frame, in "
        "2-3 short sentences. Be concrete: name objects, surfaces, "
        "people's hands and poses, tools, equipment.",
        "If the frame does not show anything meaningful (e.g. a blank "
        "wall, a person idly sitting, an empty surface), say so "
        "directly. Do NOT invent activity you cannot see.",
    ]
    if session_title:
        prompt_parts.append(f'Session title: "{session_title}".')
    if session_description:
        prompt_parts.append(f'Session description: "{session_description}".')
    prompt_parts.append(
        "End with one short line that begins with `STATE:` summarizing "
        "the scene in 6 words or less."
    )
    prompt = "\n".join(prompt_parts)

    import asyncio
    import concurrent.futures

    def _runner():
        return asyncio.run(
            asyncio.wait_for(
                vision_analyze_tool(image_url=str(path), user_prompt=prompt),
                timeout=VISION_ANALYZE_TIMEOUT_SECONDS,
            )
        )

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_runner)
            result_json = future.result(timeout=VISION_ANALYZE_TIMEOUT_SECONDS + 5)
    except concurrent.futures.TimeoutError:
        return None, f"vision_analyze timed out after {VISION_ANALYZE_TIMEOUT_SECONDS}s"
    except asyncio.TimeoutError:
        return None, f"vision_analyze timed out after {VISION_ANALYZE_TIMEOUT_SECONDS}s"
    except Exception as e:  # noqa: BLE001
        return None, f"vision_analyze failed: {e}"

    try:
        parsed = json.loads(result_json) if isinstance(result_json, str) else result_json
        if not isinstance(parsed, dict):
            return None, "vision_analyze returned non-dict result"
        if not parsed.get("success", False):
            return None, parsed.get("analysis") or "vision_analyze reported failure"
        analysis = parsed.get("analysis") or ""
        return (analysis or None), None
    except (ValueError, TypeError) as e:
        return None, f"vision_analyze response unparseable: {e}"


# ---------------------------------------------------------------------------
# STT (optional) — auxiliary model transcribes an utterance
# ---------------------------------------------------------------------------


def _transcribe_audio_safe(path: Path) -> tuple[str | None, str | None]:
    if not STT_ENABLED:
        return None, "disabled"
    try:
        from tools.transcription_tools import transcribe_audio  # type: ignore
    except Exception as e:  # noqa: BLE001
        return None, f"transcribe_audio unavailable: {e}"

    try:
        result = transcribe_audio(str(path))
    except Exception as e:  # noqa: BLE001
        return None, f"transcribe_audio raised: {e}"

    if not isinstance(result, dict):
        return None, "transcribe_audio returned non-dict"
    if not result.get("success"):
        return None, result.get("error") or "transcribe_audio failed"
    transcript = (result.get("transcript") or "").strip()
    if transcript.lower() in _STT_NOISE_BLACKLIST:
        return None, None
    if len(transcript) < 3:
        return None, None
    return transcript, None


# ---------------------------------------------------------------------------
# TTS synthesis + queue management
# ---------------------------------------------------------------------------


_QUEUE_LOCK: threading.Lock | None = None
_QUEUE_LOCK_INIT = threading.Lock()


def _get_queue_lock() -> "threading.Lock":
    global _QUEUE_LOCK
    if _QUEUE_LOCK is None:
        with _QUEUE_LOCK_INIT:
            if _QUEUE_LOCK is None:
                _QUEUE_LOCK = threading.Lock()
    return _QUEUE_LOCK


def _atomic_queue_mutate(out_dir: Path, mutator) -> dict:
    import json as _json
    q = out_dir / "queue.json"
    lock_path = out_dir / "queue.json.lock"
    tmp_suffix = f".{os.getpid()}.{threading.get_ident()}.{int(time.time() * 1000)}"
    tmp = out_dir / f".queue.json{tmp_suffix}.tmp"

    with _get_queue_lock():
        lf = None
        try:
            try:
                lf = open(lock_path, "a+")
                try:
                    import fcntl as _fcntl
                    _fcntl.flock(lf.fileno(), _fcntl.LOCK_EX)
                except (ImportError, OSError):
                    pass
            except OSError:
                lf = None

            if q.is_file():
                try:
                    data = _json.loads(q.read_text())
                    if not isinstance(data, dict):
                        data = {}
                except (ValueError, OSError):
                    data = {}
            else:
                data = {}
            data.setdefault("pending", [])
            data.setdefault("played", [])
            data.setdefault("interrupt_epoch_ms", 0)

            result = mutator(data)
            if isinstance(result, dict):
                data = result

            tmp.write_text(_json.dumps(data, ensure_ascii=False))
            os.replace(tmp, q)
            return data
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            if lf is not None:
                try:
                    import fcntl as _fcntl
                    _fcntl.flock(lf.fileno(), _fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
                try:
                    lf.close()
                except OSError:
                    pass


def _write_mute_window(audio_out_dir: Path, muted_until_ms: int) -> None:
    try:
        target = audio_out_dir / "muted_until_ms.txt"
        tmp_suffix = f".{os.getpid()}.{threading.get_ident()}.{int(time.time() * 1000)}"
        tmp = audio_out_dir / f".muted_until_ms{tmp_suffix}.tmp"
        tmp.write_text(str(int(muted_until_ms)))
        os.replace(tmp, target)
    except OSError:
        logger.warning("failed to write mute window", exc_info=True)


def _read_mute_until_ms(audio_out_dir: Path) -> int:
    target = audio_out_dir / "muted_until_ms.txt"
    try:
        raw = target.read_text().strip()
        return int(raw) if raw else 0
    except (OSError, ValueError):
        return 0


def _record_tts_chars_window(session_state: dict, chars: int) -> tuple[bool, int]:
    now = time.monotonic()
    cutoff = now - 60.0
    history = session_state.get("_tts_char_history")
    if not isinstance(history, list):
        history = []
    history = [(t, n) for (t, n) in history if t >= cutoff]
    total = sum(n for _, n in history) + chars
    if total > TTS_MAX_CHARS_PER_MIN:
        session_state["_tts_char_history"] = history
        return False, total
    history.append((now, chars))
    session_state["_tts_char_history"] = history
    return True, total


def _synthesize_tts(text: str) -> tuple[str | None, str | None]:
    try:
        from tools.tts_tool import text_to_speech_tool  # type: ignore
    except Exception as e:  # noqa: BLE001
        return None, f"text_to_speech_tool unavailable: {e}"

    try:
        result_json = text_to_speech_tool(text)
    except Exception as e:  # noqa: BLE001
        return None, f"text_to_speech_tool raised: {e}"

    try:
        result = json.loads(result_json) if isinstance(result_json, str) else result_json
    except (ValueError, TypeError):
        return None, "text_to_speech_tool returned non-JSON"
    if not isinstance(result, dict):
        return None, "text_to_speech_tool returned non-dict"
    if not result.get("success"):
        err = result.get("error") or result.get("message") or "unknown TTS error"
        return None, f"tts failed: {err}"

    path = result.get("file_path") or result.get("path") or result.get("output_path")
    if not path:
        media = result.get("content") or result.get("result") or ""
        if isinstance(media, str) and "MEDIA:" in media:
            path = media.split("MEDIA:", 1)[1].strip().split()[0]
    if not path:
        return None, f"tts succeeded but no file_path in result: {result!r}"
    return path, None


# ---------------------------------------------------------------------------
# Frame-quality + payload
# ---------------------------------------------------------------------------


def _assess_frame_quality(data: bytes) -> dict:
    n = len(data)
    quality = {
        "size_bytes": n,
        "classification": "ok",
        "reason": None,
        "blank_threshold_bytes": BLANK_BYTES_THRESHOLD,
        "low_detail_threshold_bytes": LOW_DETAIL_BYTES_THRESHOLD,
    }
    if n < BLANK_BYTES_THRESHOLD:
        quality["classification"] = "blank"
        quality["reason"] = (
            f"Frame is only {n} bytes (threshold {BLANK_BYTES_THRESHOLD}) — "
            "almost certainly a uniform color (camera covered, tab "
            "backgrounded, screen locked, or device sleeping). DO NOT "
            "describe scene contents; tell the user their camera went "
            "dark and ask them to check it."
        )
    elif n < LOW_DETAIL_BYTES_THRESHOLD:
        quality["classification"] = "low_detail"
        quality["reason"] = (
            f"Frame is small ({n} bytes, low-detail threshold "
            f"{LOW_DETAIL_BYTES_THRESHOLD}); scene may be dark, uniform, "
            "or out of focus. Hedge any description."
        )
    return quality


def _frame_payload(path: Path, session_state: dict, *, run_vision: bool = True) -> dict:
    data = path.read_bytes()
    age = max(0.0, time.time() - path.stat().st_mtime)
    ts_ms = _frame_timestamp_ms(path)

    title = session_state.get("title")
    description = session_state.get("description")

    quality = _assess_frame_quality(data)
    is_blank = quality["classification"] == "blank"

    payload: dict = {
        "session_id": session_state.get("session_id"),
        "filename": path.name,
        "bytes": len(data),
        "age_seconds": round(age, 2),
        "timestamp_ms": ts_ms,
        "session_title": title,
        "session_description": description,
        "image_quality": quality,
    }

    warnings: list[str] = []

    if is_blank:
        no_image_msg = (
            f"[vdotool] CAMERA FEED IS BLANK. The frame on disk is "
            f"{len(data)} bytes — effectively a uniform-color image "
            "(camera covered, browser tab backgrounded, screen locked, "
            "or device sleeping). Image data DELIBERATELY OMITTED so "
            "you cannot accidentally describe scene contents. Tell the "
            "user their camera feed appears blank and ask them to wake "
            "the device / uncover the lens."
        )
        payload["image_omitted"] = True
        payload["image_omitted_reason"] = "blank_frame"
        payload["content"] = [{"type": "text", "text": no_image_msg}]
        payload["warning"] = "blank_frame"
        payload["warning_message"] = no_image_msg
        warnings.append("blank_frame")
    else:
        b64 = base64.b64encode(data).decode("ascii")
        payload["image"] = {"mime_type": "image/jpeg", "data": b64}

        vision_text: str | None = None
        vision_error: str | None = None
        if run_vision:
            vision_text, vision_error = _vision_analyze_frame(path, title, description)

        if vision_text:
            payload["vision_analysis"] = vision_text
        if vision_error:
            payload["vision_error"] = vision_error

        content_blocks: list[dict] = []

        if quality["classification"] == "low_detail":
            payload["warning"] = "low_detail_frame"
            payload["warning_message"] = quality["reason"]
            warnings.append("low_detail_frame")
            content_blocks.append({
                "type": "text",
                "text": (
                    "[vdotool] Caution: this frame is small "
                    f"({len(data)} bytes) and may be dark, uniform, or "
                    "out of focus. Hedge any description."
                ),
            })

        if vision_text:
            content_blocks.append({
                "type": "text",
                "text": (
                    "[vdotool vision] An auxiliary vision model "
                    f"described the current frame:\n\n{vision_text}\n\n"
                    "Trust this description over your own guesses."
                ),
            })
        elif vision_error:
            content_blocks.append({
                "type": "text",
                "text": (
                    f"[vdotool vision] Auxiliary vision analysis FAILED: "
                    f"{vision_error}. The image bytes are included below "
                    "but Hermes typically doesn't deliver tool-result "
                    "image blocks to the orchestrator as pixels, so do "
                    "NOT trust your own description of the scene — ask "
                    "the user instead."
                ),
            })

        content_blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": b64,
            },
        })
        payload["content"] = content_blocks

    if age > STALE_FRAME_SECONDS:
        if "warning" in payload:
            payload["warning_message"] = (
                payload["warning_message"]
                + f" Additionally, the frame is {int(age)}s old."
            )
        else:
            payload["warning"] = "stale_frame"
            payload["warning_message"] = (
                f"Latest frame is {int(age)}s old. Ask the user to "
                "check that their camera tab is still open."
            )
        warnings.append("stale_frame")

    if warnings:
        payload["warnings"] = warnings
    return payload


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def start(args: dict, *, session_state: dict, **_kwargs) -> str:
    """Start a new vdocall."""
    try:
        prior_sid = session_state.get("session_id")
        if prior_sid and not bool(args.get("force")):
            # Self-heal: stale session with no viewer and no frames
            # should not block a fresh start.
            v = _viewer.get_viewer(prior_sid)
            viewer_alive = v.is_alive() if v is not None else False
            no_frames = latest_frame_age_seconds(session_state) is None
            if not viewer_alive and no_frames:
                _viewer.stop_viewer(prior_sid)
                _purge_frames_dir(prior_sid)
                for k in session_state:
                    session_state[k] = None
                prior_sid = None

        if prior_sid and not bool(args.get("force")):
            return _err(
                "session_already_active",
                "A vdocall is already active. Pass force=true to replace "
                "it, or call vdotool_end first.",
                active_session_id=prior_sid,
            )

        if prior_sid and bool(args.get("force")):
            try:
                from . import watcher as _watcher_mod
                _watcher_mod.stop_watcher(prior_sid)
            except Exception:  # noqa: BLE001
                logger.warning("failed to stop prior watcher on force-restart", exc_info=True)
            _viewer.stop_viewer(prior_sid)
            _purge_frames_dir(prior_sid)
            for k in session_state:
                session_state[k] = None

        title = (args.get("title") or "").strip() or "vdocall"
        description = (args.get("description") or "").strip()
        camera_hint = (args.get("camera_hint") or "").strip()

        session_id = uuid.uuid4().hex
        _alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        room_id = "".join(secrets.choice(_alphabet) for _ in range(12))
        if not ROOM_ID_RE.match(room_id):
            room_id = secrets.token_hex(6)

        frames_root = _frames_root()
        try:
            frames_root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return _err("frames_dir_unavailable", f"Cannot create frames root {frames_root}: {e}")

        session_dir = frames_root / session_id
        try:
            session_dir.mkdir(parents=True, exist_ok=False)
            os.chmod(session_dir, 0o755)
        except OSError as e:
            return _err("frames_dir_create_failed", f"Cannot create session dir {session_dir}: {e}")

        push_link, view_link = _build_links(_base_url(), room_id, session_id)

        session_state["session_id"] = session_id
        session_state["room_id"] = room_id
        session_state["title"] = title
        session_state["description"] = description
        session_state["camera_hint"] = camera_hint
        session_state["push_link"] = push_link
        session_state["view_link"] = view_link
        session_state["frames_dir"] = str(session_dir)
        session_state["started_at"] = int(time.time())

        # Auto-spawn headless viewer.
        viewer_info: dict | None = None
        viewer_error: str | None = None
        auto_spawn = os.environ.get("VDOTOOL_AUTO_SPAWN_VIEWER", "1") not in ("0", "false", "no")
        if auto_spawn:
            try:
                v = _viewer.spawn_viewer(session_id, view_link, replace=True)
                viewer_info = v.status()
            except RuntimeError as e:
                viewer_error = str(e)
            except OSError as e:
                viewer_error = f"failed to launch viewer: {e}"
            except Exception as e:  # noqa: BLE001
                viewer_error = f"unexpected viewer error: {e}"

        # Voice-config introspection (provider-agnostic).
        voice_report = _voice_config.get_voice_config_report()

        # Build a message the agent can paste to the user verbatim.
        camera_hint_text = camera_hint or "a relevant area (whatever you want me to look at)"
        voice_line = ""
        if voice_report["overall_ready"]:
            voice_line = (
                "\n\nVoice is set up both ways — I'll speak any "
                "time-sensitive nudges to your phone, and if you say "
                "something out loud I'll hear it. Typing still works too."
            )
        elif voice_report["tts"].get("ready"):
            voice_line = (
                "\n\nI can speak to your phone but can't hear you via "
                "the mic right now, so just type if you want to reply."
            )
        elif voice_report["stt"].get("ready"):
            voice_line = (
                "\n\nIf you speak out loud I'll hear you through the "
                "phone's mic, but I'll reply in this chat (voice "
                "output isn't set up yet)."
            )

        message_to_send_to_user = (
            f"Session '{title}' started. Open this link on your phone:\n\n"
            f"{push_link}\n\n"
            "When prompted, allow camera access and prop the phone so "
            f"it can see {camera_hint_text}.\n\n"
            "I'll watch automatically once frames start coming in — "
            "no need to ping me, I'll chime in when I see something "
            "worth saying."
            f"{voice_line}"
        )

        response = {
            "push_link": push_link,
            "user_facing_message": message_to_send_to_user,
            "session_id": session_id,
            "room_id": room_id,
            "view_link": view_link,
            "frames_dir": str(session_dir),
            "title": title,
            "description": description,
            "camera_hint": camera_hint or None,
            "viewer": viewer_info,
            "viewer_error": viewer_error,
            "next_required_action": {
                "tool": None,
                "what_to_do": (
                    "Reply to the user with the user_facing_message "
                    "field verbatim (or paraphrased — but it MUST "
                    "contain the push_link). Then STOP the turn. Do "
                    "not call any other tool. The background frame "
                    "watcher started automatically and will inject "
                    "[vdotool auto] messages into the chat when frames "
                    "arrive."
                ),
            },
            "instructions_for_agent": (
                "MANDATORY THIS TURN: your reply MUST contain the "
                "push_link. The user_facing_message already adapts to "
                "what voice capabilities are available — don't repeat "
                "or contradict its voice sentence.\n"
                "DO NOT call vdotool_watch — it blocks the chat. The "
                "background watcher (see watcher field) auto-injects "
                "[vdotool auto] messages.\n"
                "When such an auto message arrives with an attached "
                "image, LOOK at it. If it says the frame is BLANK, do "
                "NOT describe contents — just tell the user the camera "
                "went dark.\n"
                "About voice: read voice_config.suggestion_for_user for "
                "a ready-to-speak summary. If voice_config.tts.ready is "
                "false, do NOT call vdotool_say at all this session — "
                "it will error. Stick to chat."
            ),
            "watcher": {
                "auto_started": True,
                "note": (
                    "A background daemon thread is now polling frames "
                    "and will inject '[vdotool auto]' messages into the "
                    "chat. You don't need to do anything to monitor."
                ),
            },
            "voice_config": voice_report,
        }

        return _ok(response)
    except Exception as e:  # noqa: BLE001
        return _err("plugin_error", str(e))


def get_latest_frame(args: dict, *, session_state: dict, **_kwargs) -> str:
    try:
        sid = session_state.get("session_id")
        if not sid:
            return _err("no_active_session", "No vdocall is active.")

        path = _latest_frame_path(session_state)
        if path is None:
            return _err(
                "no_frames_yet",
                "No frames have been captured yet. Make sure the user "
                "has opened the push_link on their phone.",
            )

        try:
            return _ok(_frame_payload(path, session_state))
        except OSError as e:
            return _err("read_failed", f"Could not read latest frame: {e}")
    except Exception as e:  # noqa: BLE001
        return _err("plugin_error", str(e))


def watch(args: dict, *, session_state: dict, **_kwargs) -> str:
    try:
        sid = session_state.get("session_id")
        if not sid:
            return _err("no_active_session", "No vdocall is active.")

        timeout = args.get("timeout_seconds", 10)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            return _err("invalid_timeout", "timeout_seconds must be a positive number")
        timeout = min(int(timeout), 20)

        since_ms = args.get("since_ms")
        if since_ms is not None and not isinstance(since_ms, (int, float)):
            return _err("invalid_since_ms", "since_ms must be an integer (unix milliseconds)")
        since_ms = int(since_ms) if since_ms is not None else None

        deadline = time.monotonic() + timeout
        poll_interval = 0.5
        last_seen_ts: int | None = None

        while True:
            path = _latest_frame_path(session_state)
            if path is not None:
                ts = _frame_timestamp_ms(path)
                if since_ms is None:
                    try:
                        return _ok(_frame_payload(path, session_state))
                    except OSError as e:
                        return _err("read_failed", f"Could not read frame: {e}")
                if ts is not None and ts > since_ms:
                    try:
                        return _ok(_frame_payload(path, session_state))
                    except OSError as e:
                        return _err("read_failed", f"Could not read frame: {e}")
                last_seen_ts = ts

            if time.monotonic() >= deadline:
                title = session_state.get("title") or "(untitled)"
                age = latest_frame_age_seconds(session_state)
                return _ok(
                    {
                        "timed_out": True,
                        "session_id": sid,
                        "waited_seconds": timeout,
                        "since_ms": since_ms,
                        "last_seen_ts_ms": last_seen_ts,
                        "latest_frame_age_seconds": round(age, 2) if age is not None else None,
                        "session_title": title,
                        "hint": (
                            f"No new frame in the last {timeout}s. "
                            + (
                                f"Camera feed is stale ({int(age)}s old) — "
                                "user may have moved away or closed the tab."
                                if age is not None and age > STALE_FRAME_SECONDS
                                else "User may not have moved much — watch "
                                "again or ask them what's up."
                            )
                        ),
                    }
                )

            time.sleep(poll_interval)
    except Exception as e:  # noqa: BLE001
        return _err("plugin_error", str(e))


def end(args: dict, *, session_state: dict, **_kwargs) -> str:
    try:
        sid = session_state.get("session_id")
        if not sid:
            return _err("no_active_session", "No vdocall is active.")

        title = session_state.get("title")
        started_at = session_state.get("started_at") or int(time.time())
        duration_sec = max(0, int(time.time()) - int(started_at))

        viewer_stopped = _viewer.stop_viewer(sid)

        keep_frames = bool(args.get("keep_frames"))
        purged = False
        if not keep_frames:
            purged = _purge_frames_dir(sid)

        summary = {
            "session_id": sid,
            "title": title,
            "duration_seconds": duration_sec,
            "frames_purged": purged,
            "keep_frames": keep_frames,
            "viewer_stopped": viewer_stopped,
            "user_summary": args.get("summary"),
        }
        return _ok(summary)
    except Exception as e:  # noqa: BLE001
        return _err("plugin_error", str(e))


def spawn_viewer(args: dict, *, session_state: dict, **_kwargs) -> str:
    try:
        sid = session_state.get("session_id")
        if not sid:
            return _err("no_active_session", "No vdocall is active.")
        view_link = session_state.get("view_link")
        if not view_link:
            return _err("missing_view_link", "Active session has no view_link; bug.")

        replace = args.get("replace", True)
        if not isinstance(replace, bool):
            return _err("invalid_replace", "replace must be a boolean")

        try:
            v = _viewer.spawn_viewer(sid, view_link, replace=bool(replace))
        except RuntimeError as e:
            return _err("chromium_not_available", str(e))
        except OSError as e:
            return _err("spawn_failed", f"failed to launch viewer: {e}")

        return _ok({"spawned": True, "viewer": v.status()})
    except Exception as e:  # noqa: BLE001
        return _err("plugin_error", str(e))


def say(args: dict, *, session_state: dict, **_kwargs) -> str:
    """Queue text for TTS delivery to the phone speaker."""
    try:
        if not TTS_ENABLED:
            return _err(
                "tts_disabled",
                "TTS is disabled via VDOTOOL_TTS_ENABLED=0. Falling "
                "back: the user can still read your chat reply.",
            )
        sid = session_state.get("session_id")
        if not sid:
            return _err("no_active_session", "No vdocall is active.")

        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            return _err("empty_text", "text must be a non-empty string")
        text = text.strip()
        if len(text) > TTS_MAX_CHARS:
            logger.info("vdotool_say: truncating text from %d to %d chars", len(text), TTS_MAX_CHARS)
            text = text[:TTS_MAX_CHARS]

        allowed, chars_after = _record_tts_chars_window(session_state, len(text))
        if not allowed:
            return _err(
                "tts_rate_limited",
                f"TTS rate limit exceeded: would use {chars_after} chars in the "
                f"last 60s (cap {TTS_MAX_CHARS_PER_MIN}). Try again later or "
                "send the message as normal chat text.",
                chars_last_60s=chars_after,
                cap_per_60s=TTS_MAX_CHARS_PER_MIN,
                fallback_hint="Respond with a short chat message instead.",
            )

        interrupt = bool(args.get("interrupt", False))

        session_dir = _session_dir(sid)
        out_dir = session_dir / "audio_out"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return _err("audio_dir_create_failed", f"Cannot create audio dir: {e}")

        mp3_path_str, tts_err = _synthesize_tts(text)
        if tts_err:
            hint = (
                "TTS provider unavailable. Send the message as a normal "
                "chat reply; the user can still read it. Do NOT retry "
                "immediately in a loop."
            )
            lower = (tts_err or "").lower()
            if any(kw in lower for kw in ("api key", "api_key", "apikey", "unauthorized", "401", "403", "not set", "no api")):
                hint = (
                    "TTS provider API key is missing or invalid. Tell "
                    "the user that the TTS provider configured in "
                    "~/.hermes/config.yaml needs its key exported (e.g. "
                    "XAI_API_KEY for xAI, ELEVENLABS_API_KEY for "
                    "ElevenLabs, VOICE_TOOLS_OPENAI_KEY for OpenAI). For "
                    "a zero-key setup, switch tts.provider to 'edge' "
                    "(free Microsoft voices). Meanwhile continue in chat."
                )
            return _err("tts_failed", tts_err, fallback_hint=hint)

        mp3_src = Path(mp3_path_str)
        if not mp3_src.is_file():
            return _err("tts_output_missing", f"TTS reported success but no file at {mp3_src}")

        ts_ms = int(time.time() * 1000)
        ext = mp3_src.suffix or ".mp3"
        clip_name = f"{ts_ms}{ext}"
        dst = out_dir / clip_name
        try:
            shutil.move(str(mp3_src), str(dst))
        except OSError:
            try:
                shutil.copy2(str(mp3_src), str(dst))
            except OSError as e:
                return _err("audio_move_failed", f"Could not relocate TTS clip: {e}")

        try:
            size = dst.stat().st_size
        except OSError:
            size = 0
        est_duration_ms = max(500, int(size / 16) if size else 0)

        def _mutate(data: dict) -> None:
            if interrupt:
                data["pending"] = []
                data["interrupt_epoch_ms"] = ts_ms
            data["pending"].append({
                "clip": clip_name,
                "text": text,
                "queued_at": ts_ms,
                "bytes": size,
                "est_duration_ms": est_duration_ms,
                "interrupt": interrupt,
            })

        try:
            data = _atomic_queue_mutate(out_dir, _mutate)
        except OSError as e:
            return _err("queue_write_failed", f"Could not update queue.json: {e}")

        prior_mute_until = _read_mute_until_ms(out_dir)
        playback_start_ms = max(ts_ms, prior_mute_until)
        new_mute_until = playback_start_ms + est_duration_ms + 600
        _write_mute_window(out_dir, new_mute_until)

        logger.info(
            "vdotool_say queued: session=%s clip=%s bytes=%d interrupt=%s "
            "muted_until_ms=%d chars_last_60s=%d",
            sid, clip_name, size, interrupt, new_mute_until, chars_after,
        )

        return _ok({
            "queued_as": clip_name,
            "clip_path": str(dst),
            "bytes": size,
            "est_duration_ms": est_duration_ms,
            "interrupt": interrupt,
            "text": text,
            "pending_count": len(data["pending"]),
        })
    except Exception as e:  # noqa: BLE001
        return _err("plugin_error", str(e))


def get_status(args: dict, *, session_state: dict, **_kwargs) -> str:
    try:
        if not session_state.get("session_id"):
            return _ok({"active": False})

        title = session_state.get("title")
        description = session_state.get("description")

        age = latest_frame_age_seconds(session_state)

        viewer_state: dict | None = None
        v = _viewer.get_viewer(session_state["session_id"])
        if v is not None:
            viewer_state = v.status()

        stack_state: dict | None = None
        try:
            from . import stack as _stack_mod
            stack_state = _stack_mod.health_check()
        except Exception:  # noqa: BLE001
            stack_state = None

        listener_state: dict | None = None
        try:
            sid = session_state["session_id"]
            in_dir = _session_dir(sid) / "audio_in"
            status_path = in_dir / ".listener_status.json"
            if status_path.is_file():
                listener_state = json.loads(status_path.read_text())
        except (OSError, ValueError):
            listener_state = None

        return _ok({
            "active": True,
            "session_id": session_state.get("session_id"),
            "room_id": session_state.get("room_id"),
            "title": title,
            "description": description,
            "latest_frame_age_seconds": round(age, 2) if age is not None else None,
            "stale_frame": (age is not None and age > STALE_FRAME_SECONDS),
            "push_link": session_state.get("push_link"),
            "view_link": session_state.get("view_link"),
            "started_at": session_state.get("started_at"),
            "frames_dir": session_state.get("frames_dir"),
            "viewer": viewer_state,
            "chromium_available": _viewer.has_chromium(),
            "stack": stack_state,
            "listener_status": listener_state,
            "voice_config": _voice_config.get_voice_config_report(),
        })
    except Exception as e:  # noqa: BLE001
        return _err("plugin_error", str(e))


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _purge_frames_dir(session_id: str | None) -> bool:
    if not session_id or not SESSION_ID_RE.match(session_id):
        return False
    try:
        target = _session_dir(session_id)
    except (ValueError, RuntimeError):
        return False
    if not target.is_dir():
        return False
    try:
        shutil.rmtree(target)
        return True
    except OSError:
        return False
