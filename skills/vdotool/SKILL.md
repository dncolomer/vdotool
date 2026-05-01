# vdotool Agent Skill

You are an AI agent with access to **vdotool** — a live two-way WebRTC session with a remote device (almost always the user's phone). Through vdotool you can see the user's environment via the phone camera, optionally speak to them via the phone's speaker, and optionally hear them via the phone's mic. The transport is a bidirectional VDO.Ninja WebRTC room; audio and video flow both ways over the same connection.

Use it whenever live human-in-the-loop context would make you more useful: tutoring, cooking, workouts, assembly, DIY help, remote expert walkthroughs, live captioning/transcription, accessibility support, anything where "I need to SEE what you're doing" or "I want to talk you through this hands-free" beats typing.

---

## CRITICAL RULES — Read These First

1. **Do NOT rush into `vdotool_start`.** When the user asks for something that sounds like it needs live context ("help me put this chair together", "can you watch me do this form", "I want to cook X hands-free"), FIRST have a short conversation. Confirm what they want to accomplish, what they have in front of them, how much time they've got, and whether they want voice or just chat+frames. Then start the session.

2. **AFTER `vdotool_start`, your reply MUST contain the push_link.** The tool response gives you a `user_facing_message` field — send it verbatim to the user (or paraphrase, but the link must be present). Then END the turn. If you don't share the link the user has no way to start their camera and the entire session is dead.

3. **Let the watcher come to you.** The plugin auto-starts a background watcher when you call `vdotool_start`; that watcher pushes `[vdotool auto]` messages into the chat whenever a new frame arrives or the camera state changes. You never need to poll. If you want a frame on-demand between auto messages, call `vdotool_get_latest_frame` — it returns immediately.

4. **When you receive a `[vdotool auto] ...` message with an attached image, LOOK at it.** Comment only if something is actionable. Read `image_quality.classification` first: if `blank`, the plugin has DELIBERATELY OMITTED the image — the camera is dark, so do NOT describe contents; tell the user to wake the phone. If `low_detail`, hedge. If `ok`, you can describe and respond normally.

5. **Respect voice configuration — don't assume voice works.** Every `vdotool_start` response and `vdotool_status` call includes a `voice_config` block describing what TTS/STT providers the user has configured and whether any required API keys are set. **If `voice_config.tts.ready` is false, do NOT call `vdotool_say` during this session — it will error.** If the user seems to expect voice and it's not configured, paraphrase `voice_config.suggestion_for_user` to explain what they'd need to change.

6. **Keep spoken utterances short.** `vdotool_say` is for 1-2 sentence nudges, not monologues. For long explanations, stay in chat. The tool caps each call at 800 chars and the session at 10 000 chars per rolling minute.

7. **Only speak out loud when voice adds value.** Hands-busy moments ("the pan is smoking", "turn the screw 90 degrees", "breathe out on the way up"). Chat is fine for everything else.

8. **Never assume domain expertise beyond what the user gave you.** If they asked for help with guitar chords, help with chords — don't wander into music theory lectures. If the session title is "Kallax assembly", stay on IKEA.

---

## Tool Surface (7)

| Tool | When to use |
|------|-------------|
| `vdotool_start` | Once per session, after confirming context with the user. Auto-starts the stack, spawns the viewer, and starts the background watcher. |
| `vdotool_get_latest_frame` | Get the current frame immediately, no waiting. Use mid-conversation when the user says something ambiguous or you want to check progress on demand. |
| `vdotool_say` | Speak to the user's phone. Only if `voice_config.tts.ready`. Keep it short. |
| `vdotool_status` | Cheap text-only poll — session state, viewer health, stack health, listener status, voice config. |
| `vdotool_end` | When done, when user asks to stop, or when they've gone silent for ~3 minutes. |
| `vdotool_spawn_viewer` | If the headless viewer died (e.g. Chromium crashed). |
| `vdotool_start_watcher` | Rarely; if the background watcher thread died. |

---

## How Monitoring Works — The Background Watcher

**You do NOT poll for frames, AND you usually don't need to call a tool to see them.** When `vdotool_start` succeeds, the plugin spawns a background daemon that polls the frames directory every second. When a meaningful event happens (first frame, classification change, ~10s since last update), the watcher pushes a synthetic-user message into the chat **with the latest frame attached as an image**.

- You share the push_link, end the turn.
- The user opens the link on their phone, camera comes online.
- A few seconds later: `[vdotool auto] New frame attached (timestamp_ms=..., looks ok). Session "Guitar lesson" ...`
  - The actual JPEG is attached to that same message. Hermes' built-in vision routing handles delivery to your model — pixels if you're vision-capable, auto-description if you're text-only.
- Comment ONLY if actionable. Otherwise a tiny acknowledgement ("looks good, keep going") or silence.
- Watcher cools off for ~10s; next injection comes when something meaningful happens.

### Auto-message types

| Starts with | Contains | What you do |
|-------------|----------|-------------|
| `[vdotool auto] New frame attached ... looks ok` | Text + JPEG | Look at the image. Comment only when there's signal. |
| `[vdotool auto] ... is BLANK` | Text only — NO image | Tell the user the camera went dark; ask them to wake the phone. Do NOT describe scene contents. |
| `[vdotool auto] New low-detail frame attached ...` | Text + JPEG | Hedge. Describe what you can clearly see; ask user to adjust if needed. |

### Voice messages

When a user message starts with `[vdotool voice]`, treat the rest as exactly what the user said out loud — same as them typing it, just noisier. Whisper occasionally produces garbage on kitchen/workshop noise; if a voice message is a single unrelated word and the frames show nothing changed, probably noise — say nothing.

---

## Speaking and Listening (Optional)

The session CAN be hands-free via TTS out + STT in, but voice is optional. Whether it works depends on what the user has configured in `~/.hermes/config.yaml`. The plugin is provider-agnostic — it just calls Hermes' own `text_to_speech_tool` and `transcribe_audio`, so every provider Hermes supports works here (Edge, ElevenLabs, OpenAI, MiniMax, Mistral, Gemini, xAI, Piper, KittenTTS, NeuTTS for TTS; local faster-whisper, Groq, OpenAI, Mistral, xAI for STT).

### Check voice_config before promising voice

Every `vdotool_start` response and every `vdotool_status` call includes:

```json
{
  "tts": {
    "provider": "edge",
    "ready": true,
    "needs_key": null,
    "notes": "Using Hermes' default tts provider 'edge' (free, no API key needed)..."
  },
  "stt": { ... },
  "overall_ready": true,
  "suggestion_for_user": "Voice is available both ways: ..."
}
```

Use the `suggestion_for_user` field when the user asks "can you talk to me?" / "will you hear me?" / "what can this thing do?".

### If voice isn't ready

- **Never call `vdotool_say`** — it will fail with `tts_failed`.
- Tell the user once, early in the session, what they would need to change if they want voice. Don't lecture — one sentence is enough.
- The chat window still works. That's fine. Most sessions will just be chat + frames.

### Transport detail (unchanged regardless of provider)

TTS → the plugin's chosen provider synthesizes an MP3 → MP3 lands in `audio_out/` → a hidden companion pusher iframe in the viewer (a second peer in the same VDO.Ninja room as the phone) broadcasts it over WebRTC → the phone plays it because it's another peer in the room.

STT → phone's mic pushes into the room as part of the push URL → viewer's `listener.js` detects speech on the incoming audio track → records utterances → POSTs them to the writer → watcher transcribes via the configured STT provider → injects `[vdotool voice]` messages.

Both directions ride the same bidirectional VDO.Ninja WebRTC session. No separate audio channel. No phone-side setup beyond opening the push link.

---

## Example sessions

### Example: IKEA assembly helper (voice ready)

```
User: I just got this Kallax bookshelf and the instructions are useless.

Agent: Happy to help. How many rows and columns? And do you have all the
       pieces sorted out? I'll watch and talk you through it. (waits for
       answer, then) Let's open a session — prop your phone so I can see
       the whole floor area with the pieces.

Agent [calls vdotool_start(title="Kallax assembly",
                           camera_hint="your floor with all the parts laid out")]
Agent reads voice_config → overall_ready=True
Agent (chat): "Session started — open this on your phone: <push_link>
              Prop the phone so I can see the whole floor area."

[watcher injects first frame; shows unsorted hardware on carpet]

Agent [calls vdotool_say("I see the pieces. Let's sort the dowels and cam locks first.")]

[watcher injects frame — user holding a dowel up to the camera]

Agent [calls vdotool_say("That's a dowel — you'll need eight. Separate them into one pile.")]

[user speaks]
User: "[vdotool voice] how many cam locks again"

Agent [calls vdotool_say("Eight cam locks, eight dowels, eight screws.")]
```

### Example: form check during a workout (voice ready, chatty user)

```
User: can you watch my squat form for a set

Agent: Yes. Prop your phone so I can see your whole body from the side.
       Three reps to warm up, I'll flag anything before you load weight.

Agent [calls vdotool_start(title="Squat form check",
                           camera_hint="you from the side, full body")]
Agent (chat): "Ready — open this on your phone: <push_link>
              Side profile, ideally 6 feet back. I'll chime in if I
              see something."

[watcher injects frame — user in position]

Agent [calls vdotool_say("Good setup, start when ready.")]

[watcher injects frame — mid-squat, heels slightly lifted]

Agent [calls vdotool_say("Heels coming up — push them into the floor, knees track over toes.")]
```

### Example: tutoring — no voice configured

```
User: I'm stuck on this calculus problem, can you see it?

Agent [calls vdotool_start(title="Calc help",
                           camera_hint="your notebook page")]
Agent reads voice_config → overall_ready=False (tts.provider='xai', XAI_API_KEY missing)
Agent (chat):
   Session started. Open this on your phone: <push_link>

   Heads-up: your Hermes has tts.provider=xai but no XAI_API_KEY
   exported, so I can't speak through your phone this session — we'll
   stick to this chat. If you ever want voice, either export XAI_API_KEY
   or switch tts.provider to 'edge' (free) in ~/.hermes/config.yaml.

[watcher injects frame — handwritten integral on a notebook]

Agent (chat): "I see `∫ x²·eˣ dx`. This needs integration by parts. Want
              me to walk you through the setup or check your attempt first?"
```

### Example: live captioning / accessibility (STT only)

```
User: my friend is deaf and we're trying to have a conversation in a
      noisy restaurant. Can you transcribe what she says as she speaks?

Agent: Yes. Point your phone at her and open the link. Every time she
       speaks I'll post the transcript here. You'll read it as fast as
       I can transcribe. Want me to summarize long bits, or one-to-one?

Agent [calls vdotool_start(title="Live captions for a conversation")]
Agent (chat): "Open on your phone: <push_link>
              Keep the mic toward her. I'll paste what she says here
              verbatim; you take the lead on replying to her."

[watcher eventually injects:]
User: "[vdotool voice] Have you tried the fish stew here? It's what they're known for."

Agent (chat): "She asked: 'Have you tried the fish stew here? It's what
              they're known for.'"
```

---

## Error handling

| Error | What to do |
|-------|------------|
| `session_already_active` | Pass `force=true` or call `vdotool_end` first. |
| `no_active_session` | Call `vdotool_start` first. |
| `no_frames_yet` after 60s+ | Check `vdotool_status`: if viewer is None/dead, `vdotool_spawn_viewer`. Otherwise tell the user to check their camera tab. |
| `chromium_not_available` | Operator setup issue. Tell them to install Chromium or set `VDOTOOL_CHROME_BIN`. |
| `tts_disabled` / `tts_failed` | Don't call `vdotool_say` again this session. Paraphrase `voice_config.tts.notes` to explain. |
| `tts_rate_limited` | Cool off on `vdotool_say`. Stick to chat. |
| `stale_frame` warning | Ask user to check camera / wake phone. |
| `blank_frame` warning | Camera is dark. Don't describe contents. Ask user to wake phone or uncover lens. |

---

## Good-taste defaults

- Announce the push link once. Don't repeat it every turn.
- Silence is OK. You don't have to respond to every frame. A running monologue is worse than a few well-timed comments.
- When the user asks an out-of-scope question ("by the way what year was X"), answer briefly and steer back if the session is for something specific.
- Don't narrate what you see in the frame unless the user asked ("I see you holding a screwdriver"). Narration is useful for live captioning sessions; annoying for everything else.
- End sessions when they're done. `vdotool_end` is cheap. Leaving a session running wastes the headless Chromium's memory and keeps polling disk.
