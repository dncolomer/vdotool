# LAN demo — drive vdotool from this machine, camera from a phone

Hands-on walkthrough. Run `hermes chat` on this machine; your phone (or any second device) hits a push link over LAN Wi-Fi; Hermes sees the camera, optionally speaks through the phone's speaker, and optionally hears you through its mic.

The plugin auto-starts everything. You don't need two terminals.

## Do I need an API key?

**Only if you want voice features.** The base "Hermes watches my camera and we chat in the terminal" flow works with zero extra keys.

| Want to… | Minimum setup | API key needed |
|----------|---------------|----------------|
| Have Hermes watch your camera and chat in the terminal | Enable the plugin. Use any model your Hermes is already authed with. | **None specific to this plugin** |
| Have Hermes speak to your phone (TTS out) | Set `tts.provider` in `~/.hermes/config.yaml` | Depends on provider |
| Have your phone's mic transcribed into chat messages (STT in) | Set `stt.provider` in `~/.hermes/config.yaml` | Depends on provider |

**Zero-config voice**: `tts.provider: edge` + `stt.provider: local` (via faster-whisper). Both free, no keys, no accounts.

**Recommended paid voice**: set `tts.provider: xai` + `stt.provider: xai`, then `export XAI_API_KEY=...`. One key for both, excellent quality.

Hermes voice docs: https://hermes-agent.nousresearch.com/docs/user-guide/features/tts

## One-time prerequisites

- `hermes` installed and authed with any model your usual workflow uses.
- `openssl` (used to generate a self-signed TLS cert for LAN HTTPS).
- Google Chrome or Chromium installed and on `PATH` (or set `VDOTOOL_CHROME_BIN`).
- This plugin enabled in Hermes:
  ```bash
  cd /path/to/vdotool
  ln -sfn "$(pwd)" ~/.hermes/plugins/vdotool
  hermes plugins enable vdotool
  ```
- (Optional) voice config in `~/.hermes/config.yaml`.

## Run it

```bash
hermes chat
```

One terminal. The plugin lazily spawns the HTTPS server + frame writer on your first `vdotool_start` call. First tool call takes 2-3s longer; later calls are instant.

(For VPS deployments with nginx / systemd, set `VDOTOOL_AUTOSTART_STACK=0` and run `scripts/serve_lan_https.py` yourself. The plugin will detect and adopt an externally-managed stack.)

## Talk to Hermes

Tell it what you want to do together:

> I'm assembling a bookshelf and the instructions are confusing. Can you watch and help?

or

> I'm about to do some squats — can you watch my form for a couple reps?

or

> Help me cook scrambled eggs while I have my hands full.

or

> I'm at a noisy dinner with a friend who's deaf. Can you transcribe what they say and I'll type replies?

The agent will ask what you need, call `vdotool_start`, and hand you a push link like `https://192.168.1.42:8443/?room=...&push=vt_...&webcam=1&...`.

## On the phone (or second laptop)

1. Same Wi-Fi as this machine.
2. Open the push link in a modern browser (iOS Safari, Android Chrome, desktop Chrome, etc.).
3. Accept the self-signed cert warning — "Advanced" → "Proceed". Once per browser.
4. Grant camera (and mic if you want voice input).
5. Prop the device so its camera frames whatever you want Hermes to see.

Within ~5 seconds, frames start landing and Hermes begins reacting automatically. No need to ask it to look — the watcher auto-injects on meaningful frame events.

## What you should see

- **Every ~10 seconds** (configurable via `VDOTOOL_INJECT_INTERVAL_SECONDS`): a `[vdotool auto] New frame attached ...` synthetic user message appears; the agent may comment in chat.
- **If voice is configured**: the phone plays the agent's TTS replies through its speaker.
- **When you speak** (and STT is configured): within ~1-2s a `[vdotool voice] ...` user message appears with your words. The agent replies in chat and/or TTS.
- **When the camera tab goes to sleep**: frames compress to ~4 KB; the plugin flags them as "blank" so the agent asks you to wake the phone instead of inventing contents.
- **TTS mute window**: when the agent speaks, listener.js honours a mute window so the phone's mic doesn't pick up the agent's own voice and feed it back.

## Ending

> okay we're done

The agent calls `vdotool_end`. Viewer + watcher stopped, frames + audio purged. The launcher subprocess stays up for the next vdocall (it's cheap).

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Phone says "your connection is not private" | Click Advanced → Proceed. Self-signed cert is expected. |
| Camera / mic permission denied | Accept the cert warning first, reload, then grant. |
| Hermes says `viewer_error: no chromium binary found` | Install `google-chrome` or set `VDOTOOL_CHROME_BIN`. |
| Hermes says `no_frames_yet` 30+ seconds after the phone opened the link | `pgrep -af google-chrome` — there should be a process with `vdotool=1` in its URL. Ask the agent to `vdotool_spawn_viewer` if not. |
| Phone doesn't hear TTS | Check `~/.hermes/config.yaml` has `tts:` configured and the key exported. `vdotool_status` shows voice_config. |
| Listener injects noise as voice messages | Raise the VAD threshold by appending `&vtVadThresholdDb=-38` to the push/view URL, or disable STT with `VDOTOOL_STT_ENABLED=0`. |
| TTS stops working after many calls | You hit `VDOTOOL_TTS_MAX_CHARS_PER_MIN` (default 10 000). Raise it; the cap is a safety net against prompt injection. |
| Two devices on different networks | They must be on the same LAN, or use a tunnel with a real TLS cert (cloudflared / ngrok). |

## Stopping cleanly

- Ctrl-C / `/exit` in Hermes tears down (via `atexit`) the watcher, the viewer, and the stack subprocess.
- If something got stuck (SIGKILL on Hermes): `pkill -f vdotool` and `pkill -f "user-data-dir=.*vdotool-viewers"`.
- On Linux the plugin uses `PR_SET_PDEATHSIG` so Chromium subprocesses usually die with Hermes automatically.
