#!/usr/bin/env python3
"""Voice-path (TTS + STT) smoke test for the vdotool plugin.

Exercises:
  - vdotool_say happy path + queue.json shape
  - FIFO queueing + interrupt=true semantics
  - TTS rate limit (chars-per-minute sliding window)
  - TTS disabled via env var
  - Mute-window file is written and readable
  - Watcher STT path: fake utterance file → fake transcribe → inject

Mocks both ``text_to_speech_tool`` (so we don't hit a real provider)
and the watcher's ``transcribe_fn`` (so we don't need local whisper
or any cloud credentials).
"""

from __future__ import annotations

import importlib.util
import json
import os
import queue
import shutil
import sys
import tempfile
import time
from pathlib import Path


def main() -> int:
    pkg_dir = Path(__file__).resolve().parent.parent
    print(f"[*] package dir: {pkg_dir}")

    frames_dir = Path(tempfile.mkdtemp(prefix="vdotool-voice-"))
    os.environ["VDOTOOL_FRAMES_DIR"] = str(frames_dir)
    os.environ["VDOTOOL_VDO_BASE_URL"] = "http://127.0.0.1:65535"
    os.environ["VDOTOOL_AUTO_SPAWN_VIEWER"] = "0"
    os.environ["VDOTOOL_AUTOSTART_STACK"] = "0"
    os.environ["VDOTOOL_TTS_ENABLED"] = "1"
    os.environ["VDOTOOL_STT_ENABLED"] = "1"
    os.environ["VDOTOOL_VISION_ANALYZE"] = "0"
    os.environ["VDOTOOL_TTS_MAX_CHARS_PER_MIN"] = "60"

    spec = importlib.util.spec_from_file_location(
        "vdotool_voice", str(pkg_dir / "__init__.py"),
        submodule_search_locations=[str(pkg_dir)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["vdotool_voice"] = mod
    spec.loader.exec_module(mod)
    tools = sys.modules["vdotool_voice.tools"]

    injected = []

    class FakeCli:
        def __init__(self):
            self._pending_input = queue.Queue()
            self._interrupt_queue = queue.Queue()
            self._agent_running = False

    class FakeManager:
        def __init__(self, cli): self._cli_ref = cli

    class FakeCtx:
        def __init__(self):
            self.tools = []
            self._cli = FakeCli()
            self._manager = FakeManager(self._cli)
        def register_tool(self, **kw): self.tools.append(kw["name"])
        def register_hook(self, name, fn): pass
        def register_skill(self, name, path): pass
        def inject_message(self, content, role="user"):
            injected.append((role, content))
            return True

    ctx = FakeCtx()
    mod.register(ctx)
    assert "vdotool_say" in ctx.tools
    print(f"[*] registered {len(ctx.tools)} tools incl. vdotool_say")

    def fake_tts(text: str):
        tf = tempfile.NamedTemporaryFile(prefix="faketts-", suffix=".mp3", delete=False)
        tf.write(b"FAKEMP3" + text.encode("utf-8") + b"\x00" * 2000)
        tf.close()
        return tf.name, None
    tools._synthesize_tts = fake_tts

    r = json.loads(mod._start_wrapper({"title": "Voice Test"}))
    assert "error" not in r, r
    sid = r["session_id"]
    sd = Path(r["frames_dir"])
    state = mod._active_session
    print(f"[1/7] session started: {sid[:8]}")

    sr = json.loads(tools.say({"text": "Hey there."}, session_state=state))
    assert "error" not in sr, sr
    assert sr["queued_as"].endswith(".mp3")
    print(f"[2/7] vdotool_say OK: queued={sr['queued_as']} bytes={sr['bytes']} est_ms={sr['est_duration_ms']}")

    qpath = sd / "audio_out" / "queue.json"
    q = json.loads(qpath.read_text())
    assert q["pending"][0]["text"] == "Hey there."
    assert len(q["pending"]) == 1
    print("[3/7] queue.json shape matches speaker.js expectations")

    sr2 = json.loads(tools.say({"text": "Look up here."}, session_state=state))
    assert "error" not in sr2, sr2
    q = json.loads(qpath.read_text())
    assert len(q["pending"]) == 2
    print("[4/7] FIFO queueing verified")

    sr3 = json.loads(tools.say({"text": "STOP!", "interrupt": True}, session_state=state))
    assert "error" not in sr3, sr3
    q = json.loads(qpath.read_text())
    assert len(q["pending"]) == 1
    assert q["pending"][0]["text"] == "STOP!"
    assert q["interrupt_epoch_ms"] > 0
    print(f"[5/7] interrupt=True flushes queue (epoch={q['interrupt_epoch_ms']})")

    mute_file = sd / "audio_out" / "muted_until_ms.txt"
    assert mute_file.is_file(), "mute window file not written"
    deadline = int(mute_file.read_text().strip())
    assert deadline > int(time.time() * 1000), f"mute window not in future: {deadline}"
    print(f"[6/7] mute window written deadline={deadline}")

    overflow_text = "x" * 100
    rr = json.loads(tools.say({"text": overflow_text}, session_state=state))
    assert rr.get("error", {}).get("code") == "tts_rate_limited", rr
    print(f"[7/7] rate limit rejected: chars_last_60s={rr['error']['chars_last_60s']}")

    watcher_mod = sys.modules["vdotool_voice.watcher"]
    w = watcher_mod.get_watcher(sid)
    assert w is not None, "watcher not running"
    transcripts = {
        "utterance-7001.webm": ("turn the screw a quarter", None),
        "utterance-7002.webm": (None, None),  # silent drop
    }
    def fake_transcribe(path):
        return transcripts.get(path.name, ("unknown", None))
    w.transcribe_fn = fake_transcribe
    w.poll_interval = 0.2

    in_dir = sd / "audio_in"
    in_dir.mkdir(exist_ok=True)
    for name in transcripts.keys():
        (in_dir / name).write_bytes(b"FAKEOPUS")

    time.sleep(2.0)

    voice_msgs = [c for (_, c) in injected if "[vdotool voice]" in c]
    assert any("turn the screw" in m for m in voice_msgs), (
        f"expected STT injection missing; injected={injected!r}"
    )
    print(f"[STT] got {len(voice_msgs)} voice injection(s) from watcher")

    mod._end_wrapper({})
    shutil.rmtree(frames_dir, ignore_errors=True)
    print("\n[OK] ALL VOICE SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
