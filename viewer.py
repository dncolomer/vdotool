"""Headless Chromium supervisor for vdotool.

Spawns and supervises a headless Chromium per vdocall that loads the
forked VDO.Ninja view URL, so ``capture.js`` / ``speaker.js`` /
``listener.js`` can run in a browser context on the host the Hermes
agent lives on. Without a live viewer context somewhere, no frames
get captured and no TTS audio reaches the phone.

Design:
  - One subprocess per vdocall, keyed by session_id.
  - Discover a Chromium/Chrome binary via env override or PATH search.
  - Per-session ``user-data-dir`` under
    ``$VDOTOOL_VIEWER_DATA_DIR/<session_id>`` (default under the system
    tmpdir) so concurrent viewers don't collide. Cleaned up on
    terminate.
  - stdout/stderr redirected to a per-session log file.
  - Linux: PR_SET_PDEATHSIG so the child dies with the agent.
  - Graceful teardown: SIGTERM, wait up to N seconds, SIGKILL fallback.

Stdlib only. No playwright/selenium dependency.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

LOG = logging.getLogger("vdotool.viewer")


def _preexec_detach() -> None:
    """Stub kept so Popen's ``preexec_fn`` signature stays uniform.

    Previous versions used ``PR_SET_PDEATHSIG`` here so headless
    Chromium would die when Hermes died — but PDEATHSIG triggers when
    the calling *thread* exits, not the process. Hermes invokes plugin
    handlers from worker threads that finish in seconds, and the
    kernel was SIGTERM'ing Chromium prematurely.

    Cleanup is handled by ``stop_all_viewers`` (atexit) and by the
    explicit ``stop_viewer`` call in ``vdotool_end``. Combined with
    ``start_new_session=True`` on Popen, that's sufficient.
    """
    return


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DEFAULT_CANDIDATES = (
    "google-chrome-stable",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "chrome",
)


def _viewer_data_root() -> Path:
    override = os.environ.get("VDOTOOL_VIEWER_DATA_DIR")
    if override:
        return Path(override).resolve()
    return Path(tempfile.gettempdir()) / "vdotool-viewers"


def _find_chromium() -> str | None:
    env = os.environ.get("VDOTOOL_CHROME_BIN")
    if env:
        return env if (Path(env).is_file() and os.access(env, os.X_OK)) else None
    for name in _DEFAULT_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


# ---------------------------------------------------------------------------
# Viewer
# ---------------------------------------------------------------------------


class Viewer:
    """A single headless Chromium subprocess loaded on a view URL."""

    __slots__ = ("session_id", "url", "binary", "user_data_dir", "log_path", "proc", "started_at")

    def __init__(
        self,
        session_id: str,
        url: str,
        binary: str,
        user_data_dir: Path,
        log_path: Path,
    ) -> None:
        self.session_id = session_id
        self.url = url
        self.binary = binary
        self.user_data_dir = user_data_dir
        self.log_path = log_path
        self.proc: Optional[subprocess.Popen] = None
        self.started_at: Optional[int] = None

    def start(self) -> None:
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        log_f = open(self.log_path, "ab", buffering=0)

        cmd = [
            self.binary,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-translate",
            "--mute-audio",
            "--autoplay-policy=no-user-gesture-required",
            "--use-fake-ui-for-media-stream",
            "--ignore-certificate-errors",
            "--allow-insecure-localhost",
            "--enable-logging=stderr",
            "--v=0",
            "--window-size=1280,720",
            f"--user-data-dir={self.user_data_dir}",
            self.url,
        ]

        LOG.info("spawning viewer for session %s: %s", self.session_id, " ".join(cmd))
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                preexec_fn=_preexec_detach,
            )
        finally:
            try:
                log_f.close()
            except OSError:
                pass
        self.started_at = int(time.time())

    def is_alive(self) -> bool:
        if self.proc is None:
            return False
        return self.proc.poll() is None

    def status(self) -> dict:
        alive = self.is_alive()
        return {
            "session_id": self.session_id,
            "alive": alive,
            "pid": self.proc.pid if self.proc else None,
            "returncode": None if alive or self.proc is None else self.proc.returncode,
            "binary": self.binary,
            "url": self.url,
            "user_data_dir": str(self.user_data_dir),
            "log_path": str(self.log_path),
            "started_at": self.started_at,
            "uptime_seconds": (int(time.time()) - self.started_at) if self.started_at else None,
        }

    def stop(self, term_wait_seconds: float = 5.0) -> None:
        if self.proc is None:
            return

        if self.proc.poll() is None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=term_wait_seconds)
            except subprocess.TimeoutExpired:
                LOG.warning(
                    "viewer %s did not exit on SIGTERM after %.1fs, sending SIGKILL",
                    self.session_id,
                    term_wait_seconds,
                )
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    self.proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    LOG.error("viewer %s unresponsive to SIGKILL; giving up", self.session_id)

        for attempt in range(2):
            try:
                if self.user_data_dir.exists():
                    shutil.rmtree(self.user_data_dir, ignore_errors=(attempt == 1))
                break
            except OSError:
                time.sleep(0.5)


_LOCK = threading.Lock()
_VIEWERS: dict[str, Viewer] = {}


def spawn_viewer(session_id: str, view_url: str, *, replace: bool = False) -> Viewer:
    binary = _find_chromium()
    if binary is None:
        raise RuntimeError(
            "No Chromium/Chrome binary found. Install google-chrome, chromium, "
            "or set VDOTOOL_CHROME_BIN to the full path of an executable."
        )

    with _LOCK:
        existing = _VIEWERS.get(session_id)
        if existing is not None:
            if not replace and existing.is_alive():
                return existing
            existing.stop()
            _VIEWERS.pop(session_id, None)

        root = _viewer_data_root()
        root.mkdir(parents=True, exist_ok=True)
        user_data_dir = root / session_id
        log_path = root / f"{session_id}.log"

        viewer = Viewer(
            session_id=session_id,
            url=view_url,
            binary=binary,
            user_data_dir=user_data_dir,
            log_path=log_path,
        )
        viewer.start()
        _VIEWERS[session_id] = viewer
        return viewer


def get_viewer(session_id: str) -> Optional[Viewer]:
    with _LOCK:
        return _VIEWERS.get(session_id)


def stop_viewer(session_id: str) -> bool:
    with _LOCK:
        viewer = _VIEWERS.pop(session_id, None)
    if viewer is None:
        return False
    viewer.stop()
    return True


def stop_all_viewers() -> int:
    with _LOCK:
        sessions = list(_VIEWERS.keys())
        viewers = [_VIEWERS.pop(sid) for sid in sessions]
    for v in viewers:
        try:
            v.stop()
        except Exception as e:  # noqa: BLE001
            LOG.warning("failed to stop viewer %s: %s", v.session_id, e)
    return len(viewers)


def sweep_orphan_user_data_dirs() -> int:
    """Remove user-data-dirs for viewers the current process doesn't track."""
    root = _viewer_data_root()
    if not root.is_dir():
        return 0
    removed = 0
    for entry in root.iterdir():
        try:
            if entry.is_dir():
                log_path = root / f"{entry.name}.log"
                if log_path.is_file():
                    try:
                        age = time.time() - log_path.stat().st_mtime
                    except OSError:
                        continue
                    if age < 3600:
                        continue
                shutil.rmtree(entry, ignore_errors=True)
                if not entry.exists():
                    removed += 1
            elif entry.is_file() and entry.suffix == ".log":
                try:
                    if entry.stat().st_size > 5 * 1024 * 1024:
                        with open(entry, "wb"):
                            pass
                        LOG.info("truncated oversized viewer log: %s", entry)
                except OSError:
                    continue
        except OSError:
            continue
    if removed:
        LOG.info("swept %d orphan viewer user-data-dirs from %s", removed, root)
    return removed


def has_chromium() -> bool:
    return _find_chromium() is not None
