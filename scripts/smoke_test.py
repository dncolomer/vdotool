#!/usr/bin/env python3
"""Local smoke test for the vdotool plugin.

Runs the full handler lifecycle (including a real Chromium
subprocess) without any Hermes host involved. Verifies the plugin
can start a vdocall, hand out push/view links, auto-spawn the
viewer, read frames, and clean up.

    cd /path/to/vdotool
    python3 scripts/smoke_test.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def main() -> int:
    pkg_dir = Path(__file__).resolve().parent.parent
    print(f"[*] package dir: {pkg_dir}")

    frames_dir = tempfile.mkdtemp(prefix="vdotool-smoke-frames-")
    viewer_dir = tempfile.mkdtemp(prefix="vdotool-smoke-viewer-")
    os.environ["VDOTOOL_FRAMES_DIR"] = frames_dir
    os.environ["VDOTOOL_VIEWER_DATA_DIR"] = viewer_dir
    os.environ["VDOTOOL_AUTOSTART_STACK"] = "0"

    hits: list[str] = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_kw):
            pass

        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            body = b"<html><body>vdotool smoke test</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["VDOTOOL_VDO_BASE_URL"] = f"http://127.0.0.1:{port}"
    print(f"[*] stub VDO.Ninja on http://127.0.0.1:{port}")

    spec = importlib.util.spec_from_file_location(
        "vdotool_pkg",
        pkg_dir / "__init__.py",
        submodule_search_locations=[str(pkg_dir)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["vdotool_pkg"] = mod
    spec.loader.exec_module(mod)

    tools = sys.modules["vdotool_pkg.tools"]
    viewer = sys.modules["vdotool_pkg.viewer"]
    state = mod._active_session

    print(f"[*] chromium available: {viewer.has_chromium()}")
    if not viewer.has_chromium():
        print("[!] No Chromium binary found; install google-chrome or set VDOTOOL_CHROME_BIN.")
        return 2

    print("\n[1/6] vdotool_start")
    r = json.loads(mod._start_wrapper({"title": "Smoke Test", "description": "local smoke"}))
    assert "error" not in r, r
    assert r["viewer_error"] is None, f"viewer_error: {r['viewer_error']}"
    assert r["viewer"]["alive"] is True
    sid = r["session_id"]
    print(f"    session_id={sid[:12]}... viewer pid={r['viewer']['pid']}")
    print(f"    push_link={r['push_link']}")

    print("\n[2/6] waiting for Chromium to load the view URL")
    deadline = time.time() + 10.0
    while not hits and time.time() < deadline:
        time.sleep(0.2)
    assert hits, "Chromium never loaded the view URL"
    print(f"    Chromium loaded: {hits[0]}")

    print("\n[3/6] vdotool_status")
    s = json.loads(tools.get_status({}, session_state=state))
    assert s["active"] is True
    assert s["viewer"]["alive"] is True
    assert s["chromium_available"] is True
    print(f"    viewer uptime={s['viewer']['uptime_seconds']}s frame_age={s['latest_frame_age_seconds']}")

    print("\n[4/6] simulate a frame arriving, vdotool_get_latest_frame")
    session_dir = Path(r["frames_dir"])
    ts_ms = int(time.time() * 1000)
    fake = session_dir / f"frame-{ts_ms}.jpg"
    # >= 30KB to classify as 'ok'
    fake.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * (40 * 1024))
    os.symlink(fake.name, session_dir / "latest.jpg")
    g = json.loads(tools.get_latest_frame({}, session_state=state))
    assert "error" not in g, g
    assert g["session_title"] == "Smoke Test"
    assert g["image_quality"]["classification"] == "ok", g["image_quality"]
    assert g["image"]["mime_type"] == "image/jpeg"
    print(f"    frame age={g['age_seconds']}s bytes={g['bytes']} class={g['image_quality']['classification']}")

    blank = session_dir / f"frame-{ts_ms + 1}.jpg"
    blank.write_bytes(b"\xff\xd8\xff\xe0blank-frame-tiny")
    (session_dir / "latest.jpg").unlink()
    os.symlink(blank.name, session_dir / "latest.jpg")
    b = json.loads(tools.get_latest_frame({}, session_state=state))
    assert b["image_quality"]["classification"] == "blank"
    assert b.get("image_omitted") is True
    assert "image" not in b
    print(f"    blank frame correctly stripped: image_omitted=True warning={b['warning']}")

    print("\n[5/6] vdotool_status reflects blank frame")
    s2 = json.loads(tools.get_status({}, session_state=state))
    # voice_config is always present
    assert "voice_config" in s2
    print(f"    voice_config.overall_ready={s2['voice_config']['overall_ready']}")

    print("\n[6/6] vdotool_end (kills viewer, purges frames)")
    pid = s["viewer"]["pid"]
    e = json.loads(mod._end_wrapper({}))
    assert "error" not in e, e
    assert e["viewer_stopped"] is True
    assert e["frames_purged"] is True
    time.sleep(1.0)
    try:
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert not alive, f"pid {pid} still alive"
    assert not session_dir.exists(), "frames dir not purged"
    print(f"    session cleaned up, pid {pid} terminated")

    srv.shutdown()
    print("\n[OK] ALL SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
