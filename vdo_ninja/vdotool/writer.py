#!/usr/bin/env python3
"""vdotool frame writer sidecar.

Tiny stdlib HTTP server that:
  - Accepts multipart frame POSTs from the forked VDO.Ninja ``capture.js``
    and writes JPEGs under ``$VDOTOOL_FRAMES_DIR/<session_id>/``.
  - Serves per-session TTS clip queues + bytes to ``speaker.js``.
  - Accepts inbound user-mic utterances from ``listener.js``.
  - Reports listener-mute-window + listener-status so the agent can
    react to camera/mic state changes.

Filesystem contract:

    $VDOTOOL_FRAMES_DIR/<session_id>/
        frame-<unix_ms>.jpg
        latest.jpg              (symlink)
        audio_out/
            queue.json          (pending TTS clips)
            queue.json.lock     (fcntl advisory lock)
            muted_until_ms.txt  (echo-suppression window)
            <unix_ms>.mp3       (TTS bytes)
        audio_in/
            utterance-<unix_ms>.webm
            .listener_status.json

The writer is strictly a shuttle: the Hermes ``vdotool`` plugin creates
the per-session directory; the writer refuses to create session dirs on
its own.

Environment:
    VDOTOOL_FRAMES_DIR         Required. Root directory for per-session dirs.
    VDOTOOL_WRITER_HOST        Default 127.0.0.1.
    VDOTOOL_WRITER_PORT        Default 8765.
    VDOTOOL_KEEP_FRAMES_MIN    Default 10.
    VDOTOOL_MAX_FRAME_BYTES    Default 4 MB.
    VDOTOOL_AUDIO_IN_MAX_BYTES Default 8 MB.
    VDOTOOL_TTS_ENABLED        "0" disables audio-queue / audio / audio-ack.
    VDOTOOL_STT_ENABLED        "0" disables audio-in.
    VDOTOOL_MAX_CONCURRENT_REQUESTS  Default 32.
    VDOTOOL_MAX_IN_FLIGHT_BYTES      Default 32 MB.

License: AGPL-3.0 (inherits from the VDO.Ninja fork this file lives in).
"""

from __future__ import annotations

import email.parser
import email.policy
import json
import logging
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer as _BaseThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

LOG = logging.getLogger("vdotool.writer")

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
CLIP_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}\.(mp3|ogg|wav|webm|m4a)$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val


FRAMES_ROOT = Path(_env("VDOTOOL_FRAMES_DIR", "/var/lib/vdotool/frames")).resolve()
HOST = _env("VDOTOOL_WRITER_HOST", "127.0.0.1")
PORT = int(_env("VDOTOOL_WRITER_PORT", "8765"))
KEEP_MIN = int(_env("VDOTOOL_KEEP_FRAMES_MIN", "10"))
MAX_BYTES = int(_env("VDOTOOL_MAX_FRAME_BYTES", str(4 * 1024 * 1024)))
AUDIO_IN_MAX_BYTES = int(_env("VDOTOOL_AUDIO_IN_MAX_BYTES", str(8 * 1024 * 1024)))


def _flag(name: str, default: str = "1") -> bool:
    return _env(name, default).strip().lower() not in ("0", "false", "no", "off", "")


TTS_ENABLED = _flag("VDOTOOL_TTS_ENABLED", "1")
STT_ENABLED = _flag("VDOTOOL_STT_ENABLED", "1")

MAX_CONCURRENT_REQUESTS = int(_env("VDOTOOL_MAX_CONCURRENT_REQUESTS", "32"))
MAX_IN_FLIGHT_BYTES = int(_env("VDOTOOL_MAX_IN_FLIGHT_BYTES", str(32 * 1024 * 1024)))


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _session_dir(session_id: str) -> Path | None:
    if not SESSION_ID_RE.match(session_id):
        return None
    target = (FRAMES_ROOT / session_id).resolve()
    try:
        target.relative_to(FRAMES_ROOT)
    except ValueError:
        return None
    if not target.is_dir():
        return None
    return target


def _clip_name_safe(clip: str) -> bool:
    """Defense-in-depth: reject leading-dot, '..', and reserved metadata names."""
    if not clip:
        return False
    if not CLIP_NAME_RE.match(clip):
        return False
    if clip.startswith("."):
        return False
    if ".." in clip:
        return False
    reserved = {"queue.json", "queue.json.lock", "muted_until_ms.txt"}
    if clip.lower() in reserved:
        return False
    return True


def _audio_out_dir(session_id: str) -> Path | None:
    base = _session_dir(session_id)
    if base is None:
        return None
    out = base / "audio_out"
    try:
        out.mkdir(exist_ok=True)
    except OSError:
        return None
    try:
        out.resolve().relative_to(base)
    except ValueError:
        return None
    return out


def _audio_in_dir(session_id: str) -> Path | None:
    base = _session_dir(session_id)
    if base is None:
        return None
    in_ = base / "audio_in"
    try:
        in_.mkdir(exist_ok=True)
    except OSError:
        return None
    try:
        in_.resolve().relative_to(base)
    except ValueError:
        return None
    return in_


# ---------------------------------------------------------------------------
# Queue management
# ---------------------------------------------------------------------------


def _load_queue(out_dir: Path) -> dict:
    q = out_dir / "queue.json"
    if not q.is_file():
        return {"pending": [], "played": [], "interrupt_epoch_ms": 0}
    try:
        data = json.loads(q.read_text())
        if not isinstance(data, dict):
            return {"pending": [], "played": [], "interrupt_epoch_ms": 0}
        data.setdefault("pending", [])
        data.setdefault("played", [])
        data.setdefault("interrupt_epoch_ms", 0)
        return data
    except Exception:  # noqa: BLE001
        return {"pending": [], "played": [], "interrupt_epoch_ms": 0}


def _save_queue(out_dir: Path, data: dict) -> None:
    q = out_dir / "queue.json"
    tmp_suffix = f".{os.getpid()}.{threading.get_ident()}.{int(time.time() * 1000)}"
    tmp = out_dir / f".queue.json{tmp_suffix}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(tmp, q)


_QUEUE_MUTATION_LOCK = threading.Lock()


def _atomic_queue_mutate(out_dir: Path, mutator) -> dict:
    """fcntl-locked load-mutate-save on queue.json for writer + plugin safety."""
    lock_path = out_dir / "queue.json.lock"
    with _QUEUE_MUTATION_LOCK:
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

            data = _load_queue(out_dir)
            result = mutator(data)
            if isinstance(result, dict):
                data = result
            _save_queue(out_dir, data)
            return data
        finally:
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


# ---------------------------------------------------------------------------
# Frame writing
# ---------------------------------------------------------------------------


def _prune_old(session_dir: Path) -> None:
    if KEEP_MIN <= 0:
        return
    cutoff = time.time() - (KEEP_MIN * 60)
    for entry in session_dir.iterdir():
        if entry.name == "latest.jpg":
            continue
        if not entry.is_file():
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
        except OSError:
            continue


def _write_frame(session_dir: Path, data: bytes) -> Path:
    ts_ms = int(time.time() * 1000)
    frame_path = session_dir / f"frame-{ts_ms}.jpg"
    unique = f"{os.getpid()}.{threading.get_ident()}"
    tmp_path = session_dir / f".frame-{ts_ms}.{unique}.tmp"
    with open(tmp_path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, frame_path)

    link_path = session_dir / "latest.jpg"
    link_tmp = session_dir / f".latest.jpg.{ts_ms}.{unique}.tmp"
    try:
        if link_tmp.is_symlink() or link_tmp.exists():
            link_tmp.unlink()
        os.symlink(frame_path.name, link_tmp)
        os.replace(link_tmp, link_path)
    except OSError:
        try:
            with open(frame_path, "rb") as src, open(link_tmp, "wb") as dst:
                dst.write(src.read())
            os.replace(link_tmp, link_path)
        except OSError as e:
            LOG.warning("latest.jpg update failed: %s", e)

    return frame_path


def _extract_frame_bytes(body: bytes, content_type: str) -> bytes | None:
    """Extract the 'frame' field's raw bytes from a multipart body (stdlib only)."""
    header = b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n"
    parser = email.parser.BytesParser(policy=email.policy.default)
    msg = parser.parsebytes(header + body)

    if not msg.is_multipart():
        return None

    candidates: list[tuple[int, bytes]] = []
    for part in msg.iter_parts():
        disp = part.get_content_disposition() or ""
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        ctype = part.get_content_type() or ""
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        if name == "frame":
            return payload
        if disp == "form-data" and (filename or ctype.startswith("image/")):
            candidates.append((1, payload))
        else:
            candidates.append((2, payload))

    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    return candidates[0][1]


def _write_audio_in(in_dir: Path, data: bytes, ext: str = "webm") -> Path:
    ts_ms = int(time.time() * 1000)
    ext = ext.lower().lstrip(".") or "webm"
    if ext not in ("webm", "ogg", "wav", "m4a", "mp3"):
        ext = "webm"
    target = in_dir / f"utterance-{ts_ms}.{ext}"
    tmp = in_dir / f".utterance-{ts_ms}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)
    return target


# ---------------------------------------------------------------------------
# Concurrency caps
# ---------------------------------------------------------------------------


_REQUEST_SEMAPHORE = threading.Semaphore(max(1, MAX_CONCURRENT_REQUESTS))
_INFLIGHT_BYTES_LOCK = threading.Lock()
_INFLIGHT_BYTES = {"value": 0}


def _acquire_inflight_bytes(n: int) -> bool:
    with _INFLIGHT_BYTES_LOCK:
        if _INFLIGHT_BYTES["value"] + n > MAX_IN_FLIGHT_BYTES:
            return False
        _INFLIGHT_BYTES["value"] += n
        return True


def _release_inflight_bytes(n: int) -> None:
    with _INFLIGHT_BYTES_LOCK:
        _INFLIGHT_BYTES["value"] = max(0, _INFLIGHT_BYTES["value"] - n)


class CappedThreadingHTTPServer(_BaseThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def process_request(self, request, client_address):  # type: ignore[override]
        acquired = _REQUEST_SEMAPHORE.acquire(blocking=True, timeout=5)
        if not acquired:
            try:
                request.close()
            except OSError:
                pass
            LOG.warning("request limit (%d) reached, dropped connection from %s",
                        MAX_CONCURRENT_REQUESTS, client_address)
            return
        try:
            super().process_request(request, client_address)
        finally:
            _REQUEST_SEMAPHORE.release()


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class VdotoolHandler(BaseHTTPRequestHandler):
    timeout = 30

    def log_message(self, fmt, *args):  # noqa: D401
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def setup(self) -> None:
        super().setup()
        try:
            self.connection.settimeout(self.timeout)
        except OSError:
            pass

    def _reply(self, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _reply_raw(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _get_session_id(self, parsed) -> str | None:
        qs = parse_qs(parsed.query)
        ids = qs.get("sessionId") or qs.get("session_id") or []
        return ids[0] if ids else None

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/vdotool/healthz", "/healthz"):
            self._reply(200, '{"ok":true,"tts":' + ("true" if TTS_ENABLED else "false")
                        + ',"stt":' + ("true" if STT_ENABLED else "false") + '}')
            return

        if path == "/vdotool/audio-queue":
            self._handle_audio_queue(parsed)
            return

        if path == "/vdotool/listener-mute":
            self._handle_listener_mute(parsed)
            return

        if path.startswith("/vdotool/audio/"):
            self._handle_audio_serve(path)
            return

        self._reply(404, '{"error":"not_found"}')

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/vdotool/frame":
            self._handle_frame_post(parsed)
            return

        if path == "/vdotool/audio-ack":
            self._handle_audio_ack(parsed)
            return

        if path == "/vdotool/audio-in":
            self._handle_audio_in(parsed)
            return

        if path == "/vdotool/listener-status":
            self._handle_listener_status(parsed)
            return

        self._reply(404, '{"error":"not_found"}')

    # -----------------------------------------------------------------
    # Handlers
    # -----------------------------------------------------------------

    def _handle_frame_post(self, parsed) -> None:
        session_id = self._get_session_id(parsed)
        if not session_id:
            self._reply(400, '{"error":"missing_session_id"}')
            return
        session_dir = _session_dir(session_id)
        if session_dir is None:
            self._reply(404, '{"error":"unknown_session"}')
            return

        length_raw = self.headers.get("Content-Length")
        try:
            length = int(length_raw) if length_raw else 0
        except ValueError:
            self._reply(400, '{"error":"bad_content_length"}')
            return
        if length <= 0 or length > MAX_BYTES:
            self._reply(413, '{"error":"payload_too_large_or_empty"}')
            return

        ctype = self.headers.get("Content-Type", "")
        if not ctype.lower().startswith("multipart/form-data"):
            self._reply(415, '{"error":"expected_multipart_form_data"}')
            return

        if not _acquire_inflight_bytes(length):
            self._reply(503, '{"error":"in_flight_bytes_limit"}')
            return
        try:
            try:
                raw_body = self.rfile.read(length)
            except Exception as e:  # noqa: BLE001
                LOG.warning("body read failed: %s", e)
                self._reply(400, '{"error":"bad_body"}')
                return

            data = _extract_frame_bytes(raw_body, ctype)
            del raw_body
        finally:
            _release_inflight_bytes(length)

        if data is None:
            self._reply(400, '{"error":"missing_or_bad_frame_field"}')
            return
        if len(data) > MAX_BYTES:
            self._reply(413, '{"error":"frame_too_large"}')
            return
        if not data:
            self._reply(400, '{"error":"empty_frame"}')
            return

        try:
            frame_path = _write_frame(session_dir, data)
            _prune_old(session_dir)
        except OSError as e:
            LOG.error("write failed for session %s: %s", session_id, e)
            self._reply(500, '{"error":"write_failed"}')
            return

        LOG.info("wrote %s (%d bytes) for session %s", frame_path.name, len(data), session_id)
        self._reply(200, f'{{"ok":true,"file":"{frame_path.name}","bytes":{len(data)}}}')

    def _handle_audio_queue(self, parsed) -> None:
        if not TTS_ENABLED:
            self._reply(503, '{"error":"tts_disabled"}')
            return
        session_id = self._get_session_id(parsed)
        if not session_id:
            self._reply(400, '{"error":"missing_session_id"}')
            return
        if _session_dir(session_id) is None:
            self._reply(404, '{"error":"unknown_session"}')
            return
        out_dir = _audio_out_dir(session_id)
        if out_dir is None:
            self._reply(500, '{"error":"audio_dir_unavailable"}')
            return

        data = _load_queue(out_dir)
        body = json.dumps(
            {
                "session_id": session_id,
                "pending": data.get("pending", []),
                "played_count": len(data.get("played", [])),
                "interrupt_epoch_ms": data.get("interrupt_epoch_ms", 0),
            },
            ensure_ascii=False,
        )
        self._reply(200, body)

    def _handle_audio_serve(self, path: str) -> None:
        if not TTS_ENABLED:
            self._reply(503, '{"error":"tts_disabled"}')
            return
        prefix = "/vdotool/audio/"
        rest = path[len(prefix):]
        parts = rest.split("/")
        if len(parts) != 2:
            self._reply(404, '{"error":"bad_audio_path"}')
            return
        session_id, clip = parts
        if not SESSION_ID_RE.match(session_id):
            self._reply(404, '{"error":"bad_session_id"}')
            return
        if not _clip_name_safe(clip):
            self._reply(404, '{"error":"bad_clip_name"}')
            return
        out_dir = _audio_out_dir(session_id)
        if out_dir is None:
            self._reply(404, '{"error":"unknown_session"}')
            return
        clip_path = (out_dir / clip).resolve()
        try:
            clip_path.relative_to(out_dir.resolve())
        except ValueError:
            self._reply(404, '{"error":"path_escape"}')
            return
        if not clip_path.is_file():
            self._reply(404, '{"error":"clip_not_found"}')
            return
        try:
            body = clip_path.read_bytes()
        except OSError as e:
            LOG.warning("audio serve failed: %s", e)
            self._reply(500, '{"error":"read_failed"}')
            return
        suffix = clip_path.suffix.lower()
        ctype = {
            ".mp3": "audio/mpeg",
            ".ogg": "audio/ogg",
            ".wav": "audio/wav",
            ".webm": "audio/webm",
            ".m4a": "audio/mp4",
        }.get(suffix, "application/octet-stream")
        self._reply_raw(200, body, ctype)

    def _handle_audio_ack(self, parsed) -> None:
        if not TTS_ENABLED:
            self._reply(503, '{"error":"tts_disabled"}')
            return
        session_id = self._get_session_id(parsed)
        if not session_id:
            self._reply(400, '{"error":"missing_session_id"}')
            return
        qs = parse_qs(parsed.query)
        clip_list = qs.get("clip") or []
        if not clip_list:
            self._reply(400, '{"error":"missing_clip"}')
            return
        clip = clip_list[0]
        if not _clip_name_safe(clip):
            self._reply(400, '{"error":"bad_clip_name"}')
            return
        out_dir = _audio_out_dir(session_id)
        if out_dir is None:
            self._reply(404, '{"error":"unknown_session"}')
            return

        moved_flag = {"moved": False}

        def _mutate(data: dict) -> None:
            pending = data.get("pending", []) or []
            played = data.get("played", []) or []
            remaining = []
            for entry in pending:
                if isinstance(entry, dict) and entry.get("clip") == clip and not moved_flag["moved"]:
                    entry = dict(entry)
                    entry["played_at"] = int(time.time() * 1000)
                    played.append(entry)
                    moved_flag["moved"] = True
                else:
                    remaining.append(entry)
            data["pending"] = remaining
            data["played"] = played[-50:]

        try:
            _atomic_queue_mutate(out_dir, _mutate)
        except OSError as e:
            LOG.warning("audio-ack queue mutation failed: %s", e)
            self._reply(500, '{"error":"queue_write_failed"}')
            return

        moved = moved_flag["moved"]
        LOG.info("audio ack: session=%s clip=%s moved=%s", session_id, clip, moved)
        self._reply(200, f'{{"ok":true,"clip":"{clip}","acknowledged":{str(moved).lower()}}}')

    def _handle_audio_in(self, parsed) -> None:
        if not STT_ENABLED:
            self._reply(503, '{"error":"stt_disabled"}')
            return
        session_id = self._get_session_id(parsed)
        if not session_id:
            self._reply(400, '{"error":"missing_session_id"}')
            return
        in_dir = _audio_in_dir(session_id)
        if in_dir is None:
            self._reply(404, '{"error":"unknown_session"}')
            return

        length_raw = self.headers.get("Content-Length")
        try:
            length = int(length_raw) if length_raw else 0
        except ValueError:
            self._reply(400, '{"error":"bad_content_length"}')
            return
        if length <= 0 or length > AUDIO_IN_MAX_BYTES:
            self._reply(413, '{"error":"payload_too_large_or_empty"}')
            return

        if not _acquire_inflight_bytes(length):
            self._reply(503, '{"error":"in_flight_bytes_limit"}')
            return
        try:
            try:
                raw_body = self.rfile.read(length)
            except Exception as e:  # noqa: BLE001
                LOG.warning("audio-in body read failed: %s", e)
                self._reply(400, '{"error":"bad_body"}')
                return

            ctype = self.headers.get("Content-Type", "").lower()
            ext = "webm"
            if "opus" in ctype or "ogg" in ctype:
                ext = "ogg"
            elif "webm" in ctype:
                ext = "webm"
            elif "wav" in ctype:
                ext = "wav"
            elif "mp4" in ctype or "m4a" in ctype:
                ext = "m4a"
            elif "mpeg" in ctype or "mp3" in ctype:
                ext = "mp3"

            try:
                path_out = _write_audio_in(in_dir, raw_body, ext=ext)
            except OSError as e:
                LOG.error("audio-in write failed for %s: %s", session_id, e)
                self._reply(500, '{"error":"write_failed"}')
                return

            body_len = len(raw_body)
            del raw_body
        finally:
            _release_inflight_bytes(length)

        LOG.info("audio-in wrote %s (%d bytes) for session %s", path_out.name, body_len, session_id)
        self._reply(200, f'{{"ok":true,"file":"{path_out.name}","bytes":{body_len}}}')

    def _handle_listener_mute(self, parsed) -> None:
        session_id = self._get_session_id(parsed)
        if not session_id:
            self._reply(400, '{"error":"missing_session_id"}')
            return
        out_dir = _audio_out_dir(session_id)
        if out_dir is None:
            self._reply(200, '{"muted_until_ms":0,"now_ms":' + str(int(time.time() * 1000)) + '}')
            return
        mute_file = out_dir / "muted_until_ms.txt"
        muted_until = 0
        try:
            raw = mute_file.read_text().strip()
            muted_until = int(raw) if raw else 0
        except (OSError, ValueError):
            muted_until = 0
        now_ms = int(time.time() * 1000)
        self._reply(200, f'{{"muted_until_ms":{muted_until},"now_ms":{now_ms}}}')

    def _handle_listener_status(self, parsed) -> None:
        session_id = self._get_session_id(parsed)
        if not session_id:
            self._reply(400, '{"error":"missing_session_id"}')
            return
        in_dir = _audio_in_dir(session_id)
        if in_dir is None:
            self._reply(404, '{"error":"unknown_session"}')
            return

        qs = parse_qs(parsed.query)
        ok_raw = (qs.get("ok") or ["true"])[0].lower()
        reason = (qs.get("reason") or [""])[0][:200]
        ok = ok_raw in ("true", "1", "yes")

        now_ms = int(time.time() * 1000)
        payload = {"ok": ok, "reason": reason, "updated_at_ms": now_ms}
        status_path = in_dir / ".listener_status.json"
        try:
            tmp = in_dir / f".listener_status.{os.getpid()}.{now_ms}.tmp"
            tmp.write_text(json.dumps(payload, ensure_ascii=False))
            os.replace(tmp, status_path)
        except OSError as e:
            LOG.warning("listener-status write failed: %s", e)
            self._reply(500, '{"error":"write_failed"}')
            return

        LOG.info("listener-status: session=%s ok=%s reason=%s", session_id, ok, reason)
        self._reply(200, '{"ok":true}')


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not FRAMES_ROOT.exists():
        LOG.error("VDOTOOL_FRAMES_DIR does not exist: %s", FRAMES_ROOT)
        return 2
    if not FRAMES_ROOT.is_dir():
        LOG.error("VDOTOOL_FRAMES_DIR is not a directory: %s", FRAMES_ROOT)
        return 2

    server = CappedThreadingHTTPServer((HOST, PORT), VdotoolHandler)
    LOG.info("vdotool writer listening on %s:%d, frames root=%s", HOST, PORT, FRAMES_ROOT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
