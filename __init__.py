"""vdotool Hermes agent plugin.

A thin live-session companion for Hermes. Pairs with the forked
VDO.Ninja under ``vdo_ninja/`` in the same repository to give the
agent two-way audio + video with a remote device (typically the
user's phone) via WebRTC.

Architecture:
  - User opens the push link on a phone → VDO.Ninja room.
  - Headless Chromium on the host joins the same room as a viewer;
    capture.js snapshots frames, speaker.js broadcasts TTS clips over
    WebRTC, listener.js records mic utterances.
  - Background watcher thread auto-injects ``[vdotool auto]`` messages
    into the chat so the agent doesn't need to poll.
  - vdotool_say synthesizes TTS via whatever provider the user
    configured in ~/.hermes/config.yaml; the watcher transcribes
    inbound utterances via whatever STT provider is configured.

Tool surface (7):
  - vdotool_start              Start a vdocall.
  - vdotool_get_latest_frame   Return the newest frame.
  - vdotool_end                End the session.
  - vdotool_status             Poll session + voice-config state.
  - vdotool_spawn_viewer       Restart the headless viewer.
  - vdotool_start_watcher      Restart the background watcher.
  - vdotool_say                Speak text to the phone via TTS.

Hook:
  - pre_llm_call               Inject per-turn session + voice context.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
from pathlib import Path

from . import schemas, tools, viewer as _viewer, watcher as _watcher, stack as _stack

logger = logging.getLogger(__name__)

# Hermes plugin context, captured at register() time so background
# threads can inject_message into the active conversation.
_ctx_ref = None


# ---------------------------------------------------------------------------
# Active session state
# ---------------------------------------------------------------------------

_active_session: dict = {
    "session_id": None,
    "room_id": None,
    "title": None,
    "description": None,
    "camera_hint": None,
    "push_link": None,
    "view_link": None,
    "frames_dir": None,
    "started_at": None,
}
_session_lock = threading.RLock()


def get_active_session() -> dict:
    return _active_session


def clear_active_session() -> None:
    for key in _active_session:
        _active_session[key] = None


# ---------------------------------------------------------------------------
# Message injection helpers
# ---------------------------------------------------------------------------


def _inject_message_safe(content: str, role: str = "user") -> bool:
    if _ctx_ref is None:
        logger.warning("inject_message: no ctx captured at register time")
        return False
    inject = getattr(_ctx_ref, "inject_message", None)
    if inject is None:
        logger.warning("inject_message: ctx has no inject_message method")
        return False
    try:
        return bool(inject(content, role=role))
    except Exception:  # noqa: BLE001
        logger.exception("inject_message raised")
        return False


def _inject_with_image_safe(content: str, image_paths: list) -> bool:
    """Inject a synthetic user message with image attachments.

    Hermes' CLI accepts a ``(text, [Path, ...])`` tuple on
    ``cli._pending_input`` / ``cli._interrupt_queue``; this is exactly
    how user-pasted images are routed. Built-in image-routing handles
    delivery to vision vs text-only models.
    """
    if not image_paths:
        return _inject_message_safe(content, role="user")

    if _ctx_ref is None:
        return False

    cli = None
    try:
        manager = getattr(_ctx_ref, "_manager", None)
        if manager is not None:
            cli = getattr(manager, "_cli_ref", None)
    except Exception:  # noqa: BLE001
        cli = None

    if cli is None:
        return _inject_message_safe(content, role="user")

    pending = getattr(cli, "_pending_input", None)
    interrupt = getattr(cli, "_interrupt_queue", None)
    if pending is None and interrupt is None:
        return _inject_message_safe(content, role="user")

    paths = [Path(p) for p in image_paths if p is not None]
    if not paths:
        return _inject_message_safe(content, role="user")

    payload = (content, paths)
    try:
        if getattr(cli, "_agent_running", False) and interrupt is not None:
            interrupt.put(payload)
        elif pending is not None:
            pending.put(payload)
        else:
            return _inject_message_safe(content, role="user")
    except Exception:  # noqa: BLE001
        logger.exception("inject_with_image: queue.put failed")
        return _inject_message_safe(content, role="user")

    return True


# ---------------------------------------------------------------------------
# Wrappers
# ---------------------------------------------------------------------------


def _start_wrapper(args: dict, **kwargs) -> str:
    """Start a vdocall, auto-start the stack, auto-spawn watcher."""
    # Ensure the LAN stack (HTTPS + writer) is up first.
    try:
        base_url, frames_dir = _stack.ensure_running()
        os.environ.setdefault("VDOTOOL_FRAMES_DIR", frames_dir)
        os.environ.setdefault("VDOTOOL_VDO_BASE_URL", base_url)
        logger.info(
            "vdotool stack ready: base_url=%s frames_dir=%s",
            os.environ["VDOTOOL_VDO_BASE_URL"],
            os.environ["VDOTOOL_FRAMES_DIR"],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "vdotool stack autostart failed (%s); proceeding with existing env", e,
        )

    result_json = tools.start(args, session_state=_active_session, **kwargs)
    try:
        result = json.loads(result_json)
        if "error" not in result:
            sid = _active_session.get("session_id")
            frames_dir = _active_session.get("frames_dir")
            logger.info(
                "vdotool session started: %s (title=%r)",
                sid, _active_session.get("title"),
            )
            if sid and frames_dir:
                try:
                    _watcher.start_watcher(
                        session_id=sid,
                        frames_dir=Path(frames_dir),
                        session_state=_active_session,
                        inject_fn=_inject_message_safe,
                        inject_with_image_fn=_inject_with_image_safe,
                        frame_quality_fn=tools._assess_frame_quality,
                        frame_payload_fn=tools._frame_payload,
                        transcribe_fn=tools._transcribe_audio_safe,
                    )
                    logger.info("vdotool watcher auto-started for %s", sid)
                except Exception:  # noqa: BLE001
                    logger.exception("failed to auto-start watcher")
    except (json.JSONDecodeError, KeyError):
        pass
    return result_json


def _end_wrapper(args: dict, **kwargs) -> str:
    sid_before = _active_session.get("session_id")
    result_json = tools.end(args, session_state=_active_session, **kwargs)
    try:
        result = json.loads(result_json)
        if "error" not in result:
            logger.info("vdotool session ended: %s", _active_session.get("session_id"))
            if sid_before:
                _watcher.stop_watcher(sid_before)
            clear_active_session()
    except (json.JSONDecodeError, KeyError):
        pass
    return result_json


def _start_watcher_wrapper(args: dict, **kwargs) -> str:
    sid = _active_session.get("session_id")
    if not sid:
        return json.dumps({"error": {"code": "no_active_session", "message": "No vdocall is active."}})
    frames_dir = _active_session.get("frames_dir")
    if not frames_dir:
        return json.dumps({"error": {"code": "no_frames_dir", "message": "Active session has no frames_dir."}})
    if _ctx_ref is None or not hasattr(_ctx_ref, "inject_message"):
        return json.dumps({"error": {"code": "inject_unavailable", "message": "Host does not support inject_message."}})
    try:
        w = _watcher.start_watcher(
            session_id=sid,
            frames_dir=Path(frames_dir),
            session_state=_active_session,
            inject_fn=_inject_message_safe,
            inject_with_image_fn=_inject_with_image_safe,
            frame_quality_fn=tools._assess_frame_quality,
            frame_payload_fn=tools._frame_payload,
            transcribe_fn=tools._transcribe_audio_safe,
        )
        return json.dumps({
            "started": True,
            "session_id": sid,
            "is_alive": w.is_alive(),
            "min_inject_interval_seconds": w.min_inject_interval,
            "poll_interval_seconds": w.poll_interval,
        })
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": {"code": "watcher_start_failed", "message": str(e)}})


# ---------------------------------------------------------------------------
# pre_llm_call hook
# ---------------------------------------------------------------------------


def _on_pre_llm_call(session_id, user_message, **kwargs):
    sess = _active_session
    if not sess.get("session_id"):
        return None

    title = sess.get("title") or "(untitled)"
    description = sess.get("description") or ""

    # Stack health probe + auto-restart.
    #
    # The probe is a TCP connect on the HTTPS + writer ports. Under
    # heavy frame/audio load the HTTPS server's worker threads can all
    # be busy and a single connect() can fail transiently — that's
    # normal, not "the stack is down". We only auto-restart after
    # ``UNHEALTHY_THRESHOLD`` consecutive bad probes across separate
    # pre_llm_call invocations.
    stack_warning = ""
    try:
        h = _stack.health_check()
        healthy = bool(h and h.get("https_reachable") and h.get("writer_reachable"))
        consecutive = _stack.record_health_probe(healthy)
        if not healthy and consecutive >= _stack.StackSupervisor.UNHEALTHY_THRESHOLD:
            reason = h.get("reason") if h else "unknown"
            logger.warning(
                "vdotool stack unhealthy for %d consecutive probes (%s); auto-restarting",
                consecutive, reason,
            )
            try:
                _stack.stop()
            except Exception:  # noqa: BLE001
                pass
            try:
                _stack.ensure_running()
                _stack.record_health_probe(True)  # reset counter after a successful restart
                stack_warning = f" (Stack subprocess was unhealthy: {reason}; auto-restarted.)"
            except Exception as e:  # noqa: BLE001
                stack_warning = (
                    f" (WARNING: stack is down: {reason}. Auto-restart failed: {e}. "
                    "Tell the user vdotool is temporarily offline.)"
                )
        elif not healthy:
            logger.info(
                "vdotool stack probe %d/%d failed (%s); not restarting yet",
                consecutive, _stack.StackSupervisor.UNHEALTHY_THRESHOLD,
                (h.get("reason") if h else "unknown"),
            )
    except Exception:  # noqa: BLE001
        pass

    frame_age = tools.latest_frame_age_seconds(sess)
    if frame_age is None:
        frame_note = "no frames yet"
    elif frame_age > 30:
        frame_note = f"latest frame is {int(frame_age)}s old (stale — ask the user to check their camera)"
    else:
        frame_note = f"latest frame age {int(frame_age)}s"

    watcher = _watcher.get_watcher(sess["session_id"]) if sess.get("session_id") else None
    if watcher and watcher.is_alive():
        watcher_note = "background watcher is running — frame updates will be auto-injected"
    else:
        watcher_note = (
            "background watcher is NOT running — call vdotool_start_watcher "
            "to enable auto-injected frame updates"
        )

    listener_note = ""
    try:
        import json as _json
        status_path = Path(sess.get("frames_dir") or "") / "audio_in" / ".listener_status.json"
        if status_path.is_file():
            info = _json.loads(status_path.read_text())
            if isinstance(info, dict) and info.get("ok") is False:
                listener_note = (
                    f" [listener] mic feed dead (reason={info.get('reason')}); "
                    "ask user to wake phone / re-grant mic permission."
                )
    except Exception:  # noqa: BLE001
        pass

    voice_note = ""
    try:
        from . import voice_config as _vc
        report = _vc.get_voice_config_report()
        if not report.get("overall_ready"):
            tts_ready = report["tts"].get("ready")
            stt_ready = report["stt"].get("ready")
            if not tts_ready and not stt_ready:
                voice_note = (
                    " [voice] Voice (TTS + STT) is not set up on this "
                    "Hermes install. Frames + chat still work; tell the "
                    "user if they ask why the phone isn't talking to them. "
                    "See vdotool_status for details."
                )
            elif not tts_ready:
                voice_note = (
                    f" [voice] TTS unavailable ({report['tts'].get('notes','')[:160]}). "
                    "vdotool_say will return tts_failed; stick to chat."
                )
            elif not stt_ready:
                voice_note = (
                    f" [voice] STT unavailable ({report['stt'].get('notes','')[:160]}). "
                    "Phone-mic transcription will NOT happen; ask the user to type."
                )
    except Exception:  # noqa: BLE001
        pass

    nudge = (
        f" {watcher_note}. Just respond to the user normally; the "
        f"watcher will inject [vdotool auto] messages when frames "
        f"arrive. Before describing any frame, READ "
        f"image_quality.classification: if 'blank' do NOT describe "
        f"scene contents (camera is covered/asleep); if 'low_detail' hedge."
    )

    title_line = f'Active vdocall "{title}"'
    if description:
        title_line += f" — {description[:80]}"

    return {
        "context": (
            f"[vdotool] {title_line}. "
            f"{frame_note}.{nudge}{stack_warning}{listener_note}{voice_note}"
        )
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_TOOL_REGISTRY = [
    ("vdotool_start", schemas.START, "_start_wrapper"),
    ("vdotool_get_latest_frame", schemas.GET_LATEST_FRAME, tools.get_latest_frame),
    ("vdotool_end", schemas.END, "_end_wrapper"),
    ("vdotool_status", schemas.STATUS, tools.get_status),
    ("vdotool_spawn_viewer", schemas.SPAWN_VIEWER, tools.spawn_viewer),
    ("vdotool_start_watcher", schemas.START_WATCHER, "_start_watcher_wrapper"),
    ("vdotool_say", schemas.SAY, tools.say),
]


def register(ctx):
    global _ctx_ref
    _ctx_ref = ctx

    _wrappers = {
        "_start_wrapper": _start_wrapper,
        "_end_wrapper": _end_wrapper,
        "_start_watcher_wrapper": _start_watcher_wrapper,
    }

    for name, schema, handler in _TOOL_REGISTRY:
        resolved = _wrappers[handler] if isinstance(handler, str) else handler

        if resolved in (
            tools.get_latest_frame,
            tools.get_status,
            tools.spawn_viewer,
            tools.say,
        ):
            bound = _bind_session_state(resolved, _active_session)
        else:
            bound = resolved

        ctx.register_tool(
            name=name,
            toolset="vdotool",
            schema=schema,
            handler=bound,
        )

    ctx.register_hook("pre_llm_call", _on_pre_llm_call)

    skills_dir = Path(__file__).parent / "skills"
    if skills_dir.exists():
        for child in sorted(skills_dir.iterdir()):
            skill_md = child / "SKILL.md"
            if child.is_dir() and skill_md.exists():
                ctx.register_skill(child.name, skill_md)
                logger.info("Registered vdotool skill: %s", child.name)

    atexit.register(_viewer.stop_all_viewers)
    atexit.register(_watcher.stop_all_watchers)
    atexit.register(_stack.stop)

    try:
        _viewer.sweep_orphan_user_data_dirs()
    except Exception:  # noqa: BLE001
        logger.exception("orphan viewer sweep failed")

    logger.info("vdotool plugin registered (%d tools)", len(_TOOL_REGISTRY))


def _bind_session_state(handler, state):
    def _bound(args: dict, **kwargs):
        return handler(args, session_state=state, **kwargs)
    _bound.__name__ = getattr(handler, "__name__", "bound_handler")
    _bound.__doc__ = handler.__doc__
    return _bound
