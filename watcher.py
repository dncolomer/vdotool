"""Background frame/STT watcher for vdotool.

The blocking alternative would peg the entire Hermes agent loop while
waiting for a new frame. This module runs a daemon thread per vdocall
that polls the frames directory in the background and uses Hermes'
``ctx.inject_message()`` to push synthetic user messages into the
conversation when something interesting happens (a fresh non-blank
frame, a stalled feed, a transcribed utterance).

That gives us:
  - User can interrupt and chat at any time.
  - Agent runs a turn naturally each time the watcher injects.
  - Multiple watcher events queue up if the agent is mid-turn.

Throttled by default (≥10s between frame injections, ≥45s between
"blank feed" reminders) so we don't flood the agent when the camera
produces identical frames.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

LOG = logging.getLogger("vdotool.watcher")


def _env_float(name: str, default: float, min_value: float = 0.5) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        LOG.warning("invalid %s=%r, using default %.1fs", name, raw, default)
        return default
    if v < min_value:
        LOG.warning("%s=%.2fs below minimum %.2fs, clamping", name, v, min_value)
        v = min_value
    if v != default:
        LOG.info("%s=%.2fs (override)", name, v)
    return v


DEFAULT_MIN_INJECT_INTERVAL = _env_float("VDOTOOL_INJECT_INTERVAL_SECONDS", 10.0, min_value=2.0)
DEFAULT_POLL_INTERVAL = _env_float("VDOTOOL_POLL_INTERVAL_SECONDS", 1.0, min_value=0.25)
DEFAULT_BLANK_REMINDER_INTERVAL = _env_float("VDOTOOL_BLANK_REMINDER_SECONDS", 45.0, min_value=5.0)
DEFAULT_STALL_THRESHOLD = 30.0


class FrameWatcher:
    """Polls the active session's frames dir and injects messages."""

    __slots__ = (
        "session_id",
        "frames_dir",
        "inject",
        "inject_with_image",
        "frame_quality_fn",
        "frame_payload_fn",
        "transcribe_fn",
        "session_state",
        "_thread",
        "_stop_evt",
        "_last_seen_ts_ms",
        "_last_inject_t",
        "_last_class",
        "_last_blank_reminder_t",
        "_stt_executor",
        "_stt_in_flight",
        "_seen_utterances",
        "_stt_sets_lock",
        "min_inject_interval",
        "poll_interval",
        "blank_reminder_interval",
    )

    def __init__(
        self,
        session_id: str,
        frames_dir: Path,
        session_state: dict,
        inject_fn: Callable[[str, str], bool],
        frame_quality_fn: Callable[[bytes], dict],
        frame_payload_fn: Callable[[Path, dict], dict],
        inject_with_image_fn: Optional[Callable[[str, list], bool]] = None,
        transcribe_fn: Optional[Callable[[Path], tuple]] = None,
        min_inject_interval: float = DEFAULT_MIN_INJECT_INTERVAL,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        blank_reminder_interval: float = DEFAULT_BLANK_REMINDER_INTERVAL,
    ) -> None:
        self.session_id = session_id
        self.frames_dir = Path(frames_dir)
        self.session_state = session_state
        self.inject = inject_fn
        self.inject_with_image = inject_with_image_fn
        self.frame_quality_fn = frame_quality_fn
        self.frame_payload_fn = frame_payload_fn
        self.transcribe_fn = transcribe_fn
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._last_seen_ts_ms: Optional[int] = None
        self._last_inject_t: float = 0.0
        self._last_class: Optional[str] = None
        self._last_blank_reminder_t: float = 0.0
        self._stt_executor = None
        self._stt_in_flight: set[str] = set()
        self._seen_utterances: set[str] = set()
        self._stt_sets_lock = threading.Lock()
        self.min_inject_interval = min_inject_interval
        self.poll_interval = poll_interval
        self.blank_reminder_interval = blank_reminder_interval

    # -----------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        if self.transcribe_fn is not None and self._stt_executor is None:
            import concurrent.futures as _cf
            self._stt_executor = _cf.ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=f"vt-stt-{self.session_id[:8]}",
            )
        self._thread = threading.Thread(
            target=self._run,
            name=f"vdotool-watcher-{self.session_id[:8]}",
            daemon=True,
        )
        self._thread.start()
        LOG.info("watcher started for session %s (dir=%s)", self.session_id, self.frames_dir)

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        if self._stt_executor is not None:
            try:
                self._stt_executor.shutdown(wait=False, cancel_futures=True)
            except Exception:  # noqa: BLE001
                pass
            self._stt_executor = None
        LOG.info("watcher stopped for session %s", self.session_id)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -----------------------------------------------------------------

    def _latest_frame(self) -> Optional[Path]:
        latest = self.frames_dir / "latest.jpg"
        if latest.is_symlink():
            try:
                resolved = latest.resolve(strict=True)
                return resolved if resolved.is_file() else None
            except OSError:
                return None
        if latest.is_file():
            return latest
        try:
            cands = sorted(
                self.frames_dir.glob("frame-*.jpg"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            return cands[0] if cands else None
        except OSError:
            return None

    def _frame_timestamp_ms(self, path: Path) -> Optional[int]:
        import re as _re
        m = _re.match(r"^frame-(\d+)\.jpg$", path.name)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
        return None

    # -----------------------------------------------------------------
    # STT sub-pipeline
    # -----------------------------------------------------------------

    def _scan_audio_in(self) -> None:
        if self.transcribe_fn is None or self._stt_executor is None:
            return
        in_dir = self.frames_dir / "audio_in"
        if not in_dir.is_dir():
            return
        try:
            candidates = (
                sorted(in_dir.glob("utterance-*.webm"))
                + sorted(in_dir.glob("utterance-*.ogg"))
                + sorted(in_dir.glob("utterance-*.wav"))
                + sorted(in_dir.glob("utterance-*.m4a"))
                + sorted(in_dir.glob("utterance-*.mp3"))
            )
        except OSError:
            return

        for path in candidates:
            name = path.name
            with self._stt_sets_lock:
                if name in self._seen_utterances:
                    continue
                marker = path.with_suffix(path.suffix + ".processed")
                if marker.exists():
                    self._seen_utterances.add(name)
                    continue
                if name in self._stt_in_flight:
                    continue
                self._stt_in_flight.add(name)
            self._stt_executor.submit(self._run_stt_and_inject, path)

    def _run_stt_and_inject(self, path: Path) -> None:
        name = path.name
        if not path.is_file():
            with self._stt_sets_lock:
                self._seen_utterances.add(name)
                self._stt_in_flight.discard(name)
            return
        try:
            transcript, err = self.transcribe_fn(path)
            if err:
                LOG.warning("STT error on %s: %s", name, err)
            elif transcript:
                LOG.info("STT transcript for %s: %r", name, transcript[:80])
                msg = f"[vdotool voice] {transcript}"
                try:
                    self.inject(msg, "user")
                except Exception:  # noqa: BLE001
                    LOG.exception("STT inject failed")
            else:
                LOG.info("STT returned no usable transcript for %s", name)
        except Exception:  # noqa: BLE001
            LOG.exception("STT worker crashed on %s", name)
        finally:
            try:
                marker = path.with_suffix(path.suffix + ".processed")
                marker.touch(exist_ok=True)
            except OSError:
                pass
            with self._stt_sets_lock:
                self._seen_utterances.add(name)
                self._stt_in_flight.discard(name)

    # -----------------------------------------------------------------
    # Main tick
    # -----------------------------------------------------------------

    def _run(self) -> None:
        LOG.info("watcher loop entering for %s", self.session_id)
        while not self._stop_evt.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                LOG.exception("watcher tick failed; continuing")
            self._stop_evt.wait(self.poll_interval)
        LOG.info("watcher loop exiting for %s", self.session_id)

    def _tick(self) -> None:
        if self.session_state.get("session_id") != self.session_id:
            self._stop_evt.set()
            return

        # Cheap sub-poll first so STT tasks are submitted early.
        self._scan_audio_in()

        path = self._latest_frame()
        if path is None:
            return

        ts_ms = self._frame_timestamp_ms(path)
        if ts_ms is None:
            return

        is_new_frame = (self._last_seen_ts_ms is None) or (ts_ms > self._last_seen_ts_ms)
        if is_new_frame:
            self._last_seen_ts_ms = ts_ms
        else:
            return

        try:
            data = path.read_bytes()
        except OSError:
            return
        quality = self.frame_quality_fn(data)
        cls = quality.get("classification", "ok")

        now = time.monotonic()
        title = self.session_state.get("title") or "(untitled)"
        description = self.session_state.get("description") or ""

        first_frame = self._last_class is None
        cls_changed = self._last_class is not None and cls != self._last_class

        should_inject = False
        reason = ""

        if first_frame:
            should_inject = True
            reason = "first_frame"
        elif cls_changed:
            should_inject = True
            reason = f"class_changed_{self._last_class}_to_{cls}"
        elif cls == "ok" and (now - self._last_inject_t) >= self.min_inject_interval:
            should_inject = True
            reason = "periodic_ok"
        elif cls == "blank" and (now - self._last_blank_reminder_t) >= self.blank_reminder_interval:
            should_inject = True
            reason = "blank_reminder"

        self._last_class = cls

        if not should_inject:
            return

        session_hint = f'"{title}"'
        if description:
            session_hint += f" ({description[:80]})"

        attach_image = False
        if cls == "blank":
            msg = (
                f"[vdotool auto] New frame at timestamp_ms={ts_ms} is BLANK "
                f"({len(data)} bytes — camera tab is asleep, covered, or "
                f"screen-locked). Session {session_hint}. Tell the user "
                "their camera feed went dark and ask them to wake the "
                "phone / uncover the lens. Do NOT describe any scene "
                "contents — there is nothing to see. Then go silent "
                "until the next auto message arrives."
            )
            self._last_blank_reminder_t = now
        elif cls == "low_detail":
            msg = (
                f"[vdotool auto] New low-detail frame attached "
                f"(timestamp_ms={ts_ms}, {len(data)} bytes). Session "
                f"{session_hint}. The frame may be dark or out of focus — "
                "only describe details you can clearly see. If you can't "
                "make out the scene, ask the user to adjust the camera. "
                "ONLY comment if there is something actionable. Otherwise "
                "reply with a SHORT acknowledgement (<= 8 words) or stay "
                "silent."
            )
            attach_image = True
        else:
            msg = (
                f"[vdotool auto] New frame attached (timestamp_ms={ts_ms}, "
                f"{len(data)} bytes, looks ok). Session {session_hint}. "
                "Look at the attached image and ONLY comment if there is "
                "something actionable (user's context changed, safety "
                "concern, they look stuck). If nothing actionable, reply "
                "with a SHORT acknowledgement (<= 8 words) so the user "
                "knows you're watching, or stay silent. Do not narrate "
                "every frame. You do NOT need to call "
                "vdotool_get_latest_frame — the frame is already "
                "attached."
            )
            attach_image = True

        try:
            if attach_image and self.inject_with_image is not None:
                ok = self.inject_with_image(msg, [path])
            else:
                ok = self.inject(msg, "user")
            if ok:
                self._last_inject_t = now
                LOG.info(
                    "injected (%s) for session %s: ts=%s class=%s attach_image=%s",
                    reason, self.session_id, ts_ms, cls, attach_image,
                )
            else:
                LOG.warning("inject returned False for session %s", self.session_id)
        except Exception:  # noqa: BLE001
            LOG.exception("inject failed")


# ---------------------------------------------------------------------------
# Registry — one watcher per active vdocall
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()
_WATCHERS: dict[str, FrameWatcher] = {}


def start_watcher(
    session_id: str,
    frames_dir: Path,
    session_state: dict,
    inject_fn: Callable[[str, str], bool],
    frame_quality_fn: Callable[[bytes], dict],
    frame_payload_fn: Callable[[Path, dict], dict],
    inject_with_image_fn: Optional[Callable[[str, list], bool]] = None,
    transcribe_fn: Optional[Callable[[Path], tuple]] = None,
    min_inject_interval: float = DEFAULT_MIN_INJECT_INTERVAL,
) -> FrameWatcher:
    with _LOCK:
        existing = _WATCHERS.get(session_id)
        if existing is not None and existing.is_alive():
            return existing
        if existing is not None:
            existing.stop()
        w = FrameWatcher(
            session_id=session_id,
            frames_dir=frames_dir,
            session_state=session_state,
            inject_fn=inject_fn,
            frame_quality_fn=frame_quality_fn,
            frame_payload_fn=frame_payload_fn,
            inject_with_image_fn=inject_with_image_fn,
            transcribe_fn=transcribe_fn,
            min_inject_interval=min_inject_interval,
        )
        w.start()
        _WATCHERS[session_id] = w
        return w


def stop_watcher(session_id: str) -> bool:
    with _LOCK:
        w = _WATCHERS.pop(session_id, None)
    if w is None:
        return False
    w.stop()
    return True


def get_watcher(session_id: str) -> Optional[FrameWatcher]:
    with _LOCK:
        return _WATCHERS.get(session_id)


def stop_all_watchers() -> int:
    with _LOCK:
        keys = list(_WATCHERS.keys())
        ws = [_WATCHERS.pop(k) for k in keys]
    for w in ws:
        try:
            w.stop()
        except Exception:  # noqa: BLE001
            LOG.exception("failed to stop watcher %s", w.session_id)
    return len(ws)
