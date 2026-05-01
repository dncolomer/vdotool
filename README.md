# vdotool

**Live two-way WebRTC session plugin for the [Hermes agent](https://hermes-agent.nousresearch.com).** Lets Hermes see, hear, and talk to a remote device (almost always the user's phone) through a bidirectional VDO.Ninja room. Generic — use it for anything where live human-in-the-loop context would help: tutoring, cooking, workouts, assembly, DIY, remote expert walkthroughs, live transcription, accessibility support, whatever.

The repo includes (a) a thin Hermes plugin and (b) a fork of VDO.Ninja with minimal opt-in additions that snapshot frames, play back TTS clips, and record user utterances. The plugin auto-starts the whole stack on first use so running `hermes chat` is the entire setup.

## Quickstart

```bash
# Option A — install from a git remote (recommended):
hermes plugins install <owner>/vdotool          # e.g. dncolomer/vdotool
hermes plugins enable vdotool
hermes chat

# Option B — develop locally from a checkout:
git clone https://github.com/<owner>/vdotool
cd vdotool
ln -sfn "$(pwd)" ~/.hermes/plugins/vdotool
hermes plugins enable vdotool
hermes chat
```

Then tell the agent: *"let's do X with vdotool"*, where X is anything you want live context for (watching your form on a lift, helping you wire a lamp, reading a recipe aloud while you cook, transcribing a conversation for a friend). The plugin will:

- Auto-start a self-signed HTTPS server on your LAN IP + the frame writer sidecar.
- Spawn a headless Chromium viewer per vdocall.
- Generate a push link and hand it to you. Open it on a device on the same LAN.
- Start a background thread that feeds frames, speech recognitions, and TTS replies between the agent and the phone.

See [DEMO_LAN.md](DEMO_LAN.md) for the hands-on walkthrough.

## Voice (optional)

Voice is provider-agnostic — the plugin just calls Hermes' own `text_to_speech_tool` and `transcribe_audio`, so whatever you've configured in `~/.hermes/config.yaml` under `tts:` and `stt:` is what runs. Free zero-key options (Edge TTS + local faster-whisper) work out of the box; paid providers (xAI, ElevenLabs, OpenAI, etc.) work when you export their API keys.

If voice isn't configured, the session still works — just frames + chat. The agent checks `voice_config` on every `vdotool_start` and tells the user what's available.

## Architecture

```
  ┌──────────────────────┐                 ┌──────────────────────────────┐
  │  Phone (push link)   │                 │  Host machine (hermes chat)  │
  │  camera ▲  mic ▲     │                 │                              │
  │  speaker ▼           │◀═ WebRTC ═▶ ...─│  vdo_ninja/ + vdotool/       │
  └──────────────────────┘                 │   ├─ index.html (fork)       │
                                           │   ├─ capture.js  (frames ↑)  │
                                           │   ├─ speaker.js  (TTS ↓)     │
                                           │   └─ listener.js (mic ↑)     │
                                           │                              │
                                           │  writer.py (stdlib HTTP):    │
                                           │    /vdotool/frame,           │
                                           │    /vdotool/audio-{queue,    │
                                           │      ack,in},                │
                                           │    /vdotool/listener-{mute,  │
                                           │      status}, ...            │
                                           │                              │
                                           │  Hermes plugin:              │
                                           │    stack.py   (autostart)    │
                                           │    viewer.py  (headless Ch.) │
                                           │    watcher.py (bg thread)    │
                                           │    tools.py   (say, start,…) │
                                           └──────────────────────────────┘
```

Both directions (audio + video) ride the same VDO.Ninja WebRTC room. No separate audio channel; no phone-side setup beyond opening the push link.

## Repository layout

```
vdotool/
├── README.md                        # this file
├── DEMO_LAN.md                      # hands-on walkthrough
├── LICENSE                          # MIT (plugin) / AGPL (fork)
├── plugin.yaml                      # Hermes plugin manifest
├── pyproject.toml
├── __init__.py                      # plugin entry: register(), hooks, state
├── schemas.py                       # tool schemas
├── tools.py                         # tool handlers (say, start, …)
├── viewer.py                        # headless Chromium supervisor
├── watcher.py                       # background frame/STT watcher thread
├── stack.py                         # auto-start launcher+writer subprocess
├── voice_config.py                  # TTS/STT provider introspection
├── scripts/
│   ├── serve_lan_https.py           # the launcher the stack spawns
│   ├── smoke_test.py                # session lifecycle smoke test
│   ├── smoke_test_voice.py          # TTS + STT smoke test
│   ├── check_env_consistency.py     # env vars match plugin.yaml
│   └── lint_js.sh                   # node --check on fork JS
├── skills/
│   └── vdotool/SKILL.md             # agent playbook
└── vdo_ninja/                       # forked VDO.Ninja (AGPL-3.0)
    ├── index.html                   # patched: loads vdotool scripts opt-in
    └── vdotool/
        ├── capture.js               # browser: snapshot video → /vdotool/frame
        ├── speaker.js               # browser: play TTS MP3s → WebRTC
        ├── listener.js              # browser: mic VAD → /vdotool/audio-in
        ├── writer.py                # Python stdlib HTTP sidecar
        ├── start-capture.sh
        └── README.md
```

## Tools (7)

| Tool | Purpose |
|------|---------|
| `vdotool_start` | Mint a VDO.Ninja room, auto-start the stack, spawn the viewer + watcher, return push/view links. |
| `vdotool_get_latest_frame` | Newest frame as base64 + metadata, with optional auxiliary-vision text analysis. |
| `vdotool_say` | Speak text to the phone via TTS (configured provider). Supports `interrupt=true`. |
| `vdotool_end` | Stop viewer, watcher, TTS queue; purge frames. |
| `vdotool_status` | Poll session state, viewer/stack/listener health, voice config. |
| `vdotool_spawn_viewer` | Restart the headless viewer. |
| `vdotool_start_watcher` | Restart the background watcher thread. |

## Hook

`pre_llm_call` — every turn, injects a compact status line (active session title, frame freshness, stack health, listener health, voice-config notes) so the agent stays oriented. Also cheap stack health-check + auto-restart on failure.

## Configuration

The plugin auto-starts everything. Only **two** env vars are strictly required and they're auto-filled by the stack on first start:

| Env var | Required | Default | Notes |
|---------|----------|---------|-------|
| `VDOTOOL_FRAMES_DIR` | — | auto-filled | Shared dir for frames and audio. |
| `VDOTOOL_VDO_BASE_URL` | — | auto-filled | Where the phone points. |
| `VDOTOOL_AUTOSTART_STACK` | no | `1` | Plugin manages writer + HTTPS. Set `0` if you run `scripts/serve_lan_https.py` yourself. |
| `VDOTOOL_AUTO_SPAWN_VIEWER` | no | `1` | Plugin manages headless Chromium per session. |
| `VDOTOOL_CHROME_BIN` | no | PATH search | Full path to Chromium/Chrome. |
| `VDOTOOL_VIEWER_DATA_DIR` | no | `$TMPDIR/vdotool-viewers` | Per-session user-data-dir. |
| `VDOTOOL_WRITER_HOST` | no | `127.0.0.1` | Writer bind host. |
| `VDOTOOL_WRITER_PORT` | no | `8765` | Writer bind port. |
| `VDOTOOL_MAX_FRAME_BYTES` | no | `4194304` | Per-frame cap (4 MB). |
| `VDOTOOL_KEEP_FRAMES_MIN` | no | `10` | Writer frame retention. |
| `VDOTOOL_AUDIO_IN_MAX_BYTES` | no | `8388608` | Per-utterance cap (8 MB). |
| `VDOTOOL_MAX_CONCURRENT_REQUESTS` | no | `32` | Writer thread cap. |
| `VDOTOOL_MAX_IN_FLIGHT_BYTES` | no | `33554432` | Writer memory cap (32 MB). |
| `VDOTOOL_INJECT_INTERVAL_SECONDS` | no | `10.0` | Watcher: seconds between frame injections. |
| `VDOTOOL_POLL_INTERVAL_SECONDS` | no | `1.0` | Watcher disk-poll rate. |
| `VDOTOOL_BLANK_REMINDER_SECONDS` | no | `45.0` | "Camera blank" reminder throttle. |
| `VDOTOOL_BLANK_BYTES_THRESHOLD` | no | `8000` | JPEG size below which a frame is "blank". |
| `VDOTOOL_LOW_DETAIL_BYTES_THRESHOLD` | no | `15000` | Below this = "low_detail". |
| `VDOTOOL_VISION_ANALYZE` | no | `1` | Run Hermes' aux vision model on frames. |
| `VDOTOOL_VISION_TIMEOUT` | no | `60` | Aux vision timeout (s). |
| `VDOTOOL_TTS_ENABLED` | no | `1` | Master switch for `vdotool_say`. |
| `VDOTOOL_TTS_MAX_CHARS` | no | `800` | Per-call text cap. |
| `VDOTOOL_TTS_MAX_CHARS_PER_MIN` | no | `10000` | Per-session 60s sliding-window cap. |
| `VDOTOOL_STT_ENABLED` | no | `1` | Master switch for mic → transcript. |

Full docs for each in `plugin.yaml`. Run `python3 scripts/check_env_consistency.py` to verify code and docs are in sync.

## Filesystem contract

```
$VDOTOOL_FRAMES_DIR/
└── <session_id>/                        # created by plugin on vdotool_start
    ├── frame-<unix_ms>.jpg               # fork → writer → disk
    ├── latest.jpg                        # symlink, always newest
    ├── audio_out/                        # TTS clips queued for the phone
    │   ├── queue.json
    │   ├── queue.json.lock
    │   ├── muted_until_ms.txt            # echo-loop window (listener reads)
    │   └── <unix_ms>.mp3
    └── audio_in/                         # user utterances from phone mic
        ├── utterance-<unix_ms>.webm
        ├── utterance-<unix_ms>.webm.processed
        └── .listener_status.json         # mic health surfaced to the agent
```

The writer refuses to create session dirs on its own; only the plugin does. Rate-limiting and cleanup-by-construction.

## Development

```bash
# Parse + env-var + JS consistency
python3 -c "import ast; [ast.parse(open(f).read()) for f in ['__init__.py', 'tools.py', 'schemas.py', 'viewer.py', 'watcher.py', 'stack.py', 'voice_config.py', 'vdo_ninja/vdotool/writer.py']]"
python3 scripts/check_env_consistency.py
./scripts/lint_js.sh

# Smoke tests
python3 scripts/smoke_test.py            # session lifecycle + viewer spawn
python3 scripts/smoke_test_voice.py      # TTS queue + STT injection
```

## Licensing

- Plugin code (everything outside `vdo_ninja/`) is **MIT**.
- The forked VDO.Ninja in `vdo_ninja/` remains **AGPL-3.0** per upstream. Changes under `vdo_ninja/vdotool/` are also AGPL-3.0.

See `vdo_ninja/AGPLv3.md` for the fork's license.

## Privacy

- The writer only accepts uploads for sessions the plugin explicitly created.
- Frames are purged `VDOTOOL_KEEP_FRAMES_MIN` minutes (default 10) after last write; the entire session dir is deleted on `vdotool_end` unless you pass `keep_frames: true`.
- Audio clips (both TTS out and mic in) follow the same retention.
- No data is sent to any third-party service by the plugin itself. Whichever TTS/STT/vision providers you configure in `~/.hermes/config.yaml` are the only outbound paths; see Hermes' own docs for how to keep everything local (Piper + faster-whisper).
