"""Auto-start/stop the local LAN stack (fork HTTPS + writer sidecar).

The vdotool plugin needs three processes running before
``vdotool_start`` can produce a working push link:

  1. The forked VDO.Ninja served over HTTPS on ``0.0.0.0:<port>``.
  2. The frame writer sidecar on ``127.0.0.1:<writer_port>``.
  3. The headless Chromium viewer (handled by ``viewer.py``).

This module manages (1) and (2) as a single subprocess — the
``scripts/serve_lan_https.py`` launcher.

Runs the stack as a subprocess (not an in-process import) so that:
  - Hermes can reload the plugin without leaking sockets/threads.
  - We have a clean kill switch.
  - PR_SET_PDEATHSIG ensures the child dies with us.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

LOG = logging.getLogger("vdotool.stack")


def _preexec_detach() -> None:
    """Detach the launcher subprocess from the calling thread on Linux.

    Originally this used ``PR_SET_PDEATHSIG`` so the launcher would die
    if Hermes died. Problem: PDEATHSIG is keyed off the calling
    *thread*, not the process. Hermes loads the plugin from a worker
    thread that finishes within seconds of the first vdotool_start
    call, and the kernel then SIGTERMs the launcher even though Hermes
    itself is alive and well. That's exactly the "launcher dies ~2s
    after first contact" bug.

    We rely on:
      - ``start_new_session=True`` (passed to Popen) to detach from the
        controlling terminal and put the launcher in its own process
        group, so it survives the parent's TTY closing.
      - ``atexit.register(_stack.stop)`` in __init__.py for clean
        shutdown when Hermes itself exits.
      - The supervisor's explicit ``stop()`` for end-of-session.

    PDEATHSIG is intentionally NOT used. Leaving this hook as a stub
    so Popen's preexec_fn signature stays uniform.
    """
    return


_REPO_ROOT = Path(__file__).resolve().parent
_LAUNCHER = _REPO_ROOT / "scripts" / "serve_lan_https.py"
_MARKER_PATH = Path("/tmp/vdotool-lan-demo.json")


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in ("0", "false", "no", "off", "")


AUTOSTART_ENABLED = _flag("VDOTOOL_AUTOSTART_STACK", "1")


class StackSupervisor:
    __slots__ = (
        "_proc", "_base_url", "_frames_dir", "_lock", "_launched_log",
        "_consecutive_unhealthy",
    )

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._base_url: Optional[str] = None
        self._frames_dir: Optional[str] = None
        self._lock = threading.Lock()
        self._launched_log: Optional[Path] = None
        # Counter so a single transient probe failure doesn't trigger
        # an auto-restart. The pre_llm_call hook checks this counter
        # rather than the raw health_check result.
        self._consecutive_unhealthy = 0

    def ensure_running(self, timeout_seconds: float = 15.0) -> tuple[str, str]:
        """Start the stack if not running; return (base_url, frames_dir)."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None and self._base_url and self._frames_dir:
                return self._base_url, self._frames_dir

            existing = self._detect_existing()
            if existing is not None:
                self._base_url, self._frames_dir = existing
                LOG.info("vdotool stack: adopting existing launcher at %s", self._base_url)
                return existing

            if not AUTOSTART_ENABLED:
                raise RuntimeError(
                    "VDOTOOL_AUTOSTART_STACK is disabled and no external "
                    "launcher is running. Start scripts/serve_lan_https.py "
                    "manually or re-enable autostart."
                )

            if not _LAUNCHER.is_file():
                raise RuntimeError(
                    f"Launcher script not found at {_LAUNCHER}. Is the "
                    "plugin installed correctly?"
                )

            try:
                _MARKER_PATH.unlink(missing_ok=True)
            except OSError:
                pass

            log_dir = Path(os.environ.get("TMPDIR", "/tmp"))
            self._launched_log = log_dir / f"vdotool-stack-{os.getpid()}.log"
            log_fd = open(self._launched_log, "ab", buffering=0)
            LOG.info("vdotool stack: starting launcher (log=%s)", self._launched_log)
            try:
                self._proc = subprocess.Popen(
                    [sys.executable, str(_LAUNCHER)],
                    stdout=log_fd,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    cwd=str(_REPO_ROOT),
                    start_new_session=True,
                    preexec_fn=_preexec_detach,
                    env=os.environ.copy(),
                )
            finally:
                try:
                    log_fd.close()
                except OSError:
                    pass

        # Release the lock during the wait.
        info = self._wait_for_marker(timeout_seconds)

        with self._lock:
            if info is None:
                tail = "(no log yet)"
                try:
                    if self._launched_log and self._launched_log.is_file():
                        with open(self._launched_log, "rb") as f:
                            tail = f.read()[-1500:].decode("utf-8", errors="replace")
                except OSError:
                    pass
                LOG.error("launcher did not produce marker within %ss. Log tail:\n%s",
                          timeout_seconds, tail)
            else:
                self._base_url, self._frames_dir = info
                LOG.info(
                    "vdotool stack ready: base_url=%s frames_dir=%s",
                    self._base_url, self._frames_dir,
                )
                return info

        self.stop()
        raise RuntimeError(
            f"vdotool launcher failed to start within {timeout_seconds}s. "
            f"See {self._launched_log} for details."
        )

    def stop(self, timeout: float = 4.0) -> None:
        # Debug-trace where the kill came from. The launcher should
        # only die at end of session, not mid-session, so any caller
        # of stop() during an active vdocall is interesting.
        import traceback
        stack_summary = "".join(traceback.format_stack(limit=8))
        with self._lock:
            proc = self._proc
            self._proc = None
            self._base_url = None
            self._frames_dir = None
        if proc is None or proc.poll() is not None:
            LOG.info("StackSupervisor.stop() called but no live proc; trace:\n%s", stack_summary)
            return
        LOG.warning(
            "StackSupervisor.stop() terminating launcher pid=%s; trace:\n%s",
            proc.pid, stack_summary,
        )
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            LOG.warning("launcher did not exit on SIGTERM after %.1fs; killing", timeout)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                LOG.error("launcher unresponsive to SIGKILL; giving up")
        try:
            _MARKER_PATH.unlink(missing_ok=True)
        except OSError:
            pass

    def is_alive(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def status(self) -> dict:
        with self._lock:
            return {
                "managed": self._proc is not None,
                "alive": self._proc is not None and self._proc.poll() is None,
                "pid": self._proc.pid if self._proc else None,
                "base_url": self._base_url,
                "frames_dir": self._frames_dir,
                "log_path": str(self._launched_log) if self._launched_log else None,
            }

    def health_check(self) -> dict:
        with self._lock:
            proc = self._proc
            base_url = self._base_url
            frames_dir = self._frames_dir
        info = {
            "managed": proc is not None,
            "alive": proc is not None and proc.poll() is None,
            "https_reachable": False,
            "writer_reachable": False,
            "base_url": base_url,
            "frames_dir": frames_dir,
            "reason": None,
        }
        if not base_url or not frames_dir:
            marker = self._read_marker()
            if marker is not None:
                base_url, frames_dir, _https_port = marker
                info["base_url"] = base_url
                info["frames_dir"] = frames_dir
        marker = self._read_marker()
        if marker is None:
            info["reason"] = "no marker file"
            return info
        _base_url, _frames_dir, https_port = marker
        writer_port = 8765
        try:
            data = json.loads(_MARKER_PATH.read_text())
            if isinstance(data.get("writer_port"), int):
                writer_port = data["writer_port"]
        except (ValueError, OSError):
            pass
        # Be generous with the connect timeout: under load, the HTTPS
        # server's 32 worker threads can all be busy serving frame
        # uploads / queue polls, and a 1s connect can fail spuriously.
        # 5s is far more than the LLM hook can afford to wait on the
        # critical path, but health_check() is supposed to be cheap so
        # we keep 2.0 here and rely on the pre_llm_call hook's
        # consecutive-failure threshold below to debounce flakes.
        info["https_reachable"] = self._port_open("127.0.0.1", https_port, timeout=2.0)
        info["writer_reachable"] = self._port_open("127.0.0.1", writer_port, timeout=2.0)
        if not info["https_reachable"]:
            info["reason"] = f"https port {https_port} not responding"
        elif not info["writer_reachable"]:
            info["reason"] = f"writer port {writer_port} not responding"
        elif info["managed"] and not info["alive"]:
            info["reason"] = "managed subprocess exited"
        return info

    # Number of consecutive failed probes before we believe the stack
    # is actually dead. One failed probe under load is normal; three in
    # a row across pre_llm_call invocations is real.
    UNHEALTHY_THRESHOLD = 3

    def record_health_probe(self, healthy: bool) -> int:
        """Update the consecutive-failure counter; return current value.

        Callers (the pre_llm_call hook) should only auto-restart when
        the returned count >= UNHEALTHY_THRESHOLD.
        """
        with self._lock:
            if healthy:
                self._consecutive_unhealthy = 0
            else:
                self._consecutive_unhealthy += 1
            return self._consecutive_unhealthy

    # -----------------------------------------------------------------

    def _wait_for_marker(self, timeout_seconds: float) -> Optional[tuple[str, str]]:
        deadline = time.monotonic() + timeout_seconds
        last_err: Optional[str] = None
        while time.monotonic() < deadline:
            time.sleep(0.25)
            info = self._read_marker()
            if info is None:
                continue
            base_url, frames_dir, https_port = info
            if self._port_open("127.0.0.1", https_port):
                return base_url, frames_dir
            last_err = f"marker points at port {https_port} but nothing listening yet"
        if last_err:
            LOG.debug("wait_for_marker last_err=%s", last_err)
        return None

    @staticmethod
    def _read_marker() -> Optional[tuple[str, str, int]]:
        if not _MARKER_PATH.is_file():
            return None
        try:
            data = json.loads(_MARKER_PATH.read_text())
        except (ValueError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        base_url = data.get("base_url")
        frames_dir = data.get("frames_dir")
        https_port = data.get("https_port")
        if not (isinstance(base_url, str) and isinstance(frames_dir, str)
                and isinstance(https_port, int)):
            return None
        return base_url, frames_dir, https_port

    @staticmethod
    def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _detect_existing(self) -> Optional[tuple[str, str]]:
        info = self._read_marker()
        if info is None:
            return None
        base_url, frames_dir, https_port = info
        if not self._port_open("127.0.0.1", https_port):
            return None
        writer_port = 8765
        try:
            m = json.loads(_MARKER_PATH.read_text())
            if isinstance(m.get("writer_port"), int):
                writer_port = m["writer_port"]
        except (ValueError, OSError):
            pass
        if not self._port_open("127.0.0.1", writer_port):
            return None
        return base_url, frames_dir


_supervisor = StackSupervisor()


def ensure_running(timeout_seconds: float = 15.0) -> tuple[str, str]:
    return _supervisor.ensure_running(timeout_seconds=timeout_seconds)


def stop() -> None:
    _supervisor.stop()


def is_alive() -> bool:
    return _supervisor.is_alive()


def status() -> dict:
    return _supervisor.status()


def health_check() -> dict:
    return _supervisor.health_check()
