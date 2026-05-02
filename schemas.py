"""Tool schemas for the vdotool Hermes plugin.

Schemas follow the OpenAI / Anthropic function-calling shape used by
other Hermes plugins.
"""

# ---------------------------------------------------------------------------
# vdotool_start
# ---------------------------------------------------------------------------

START = {
    "name": "vdotool_start",
    "description": (
        "Start a new vdocall — a live two-way WebRTC session between "
        "Hermes and a remote device (typically the user's phone). The "
        "plugin mints a random VDO.Ninja room, creates a frames + "
        "audio directory on disk, spawns a headless Chromium viewer to "
        "receive the phone's camera + mic, and returns a `push_link` "
        "you must hand to the user. Only one vdocall can be active at "
        "a time; pass force=true to replace an existing one.\n\n"
        "Use vdocalls for any live human-in-the-loop interaction where "
        "Hermes benefits from seeing and/or hearing the user: live "
        "tutoring, cooking, workouts, assembly, DIY help, remote "
        "expert walkthroughs, live transcription, etc."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": (
                    "Short human-readable title for this session "
                    "(e.g. 'Kitchen help', 'IKEA Kallax assembly', "
                    "'Guitar lesson'). Surfaced to the user and logged."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "Optional longer note about what this session is "
                    "for. Shown to the agent on every turn via the "
                    "pre_llm_call hook so you stay oriented."
                ),
            },
            "camera_hint": {
                "type": "string",
                "description": (
                    "Optional hint for the user about where to aim "
                    "their camera (e.g. 'point at your workbench', "
                    "'show me your hands', 'frame the whole stove')."
                ),
            },
            "force": {
                "type": "boolean",
                "description": (
                    "If true, end any currently-active vdocall before "
                    "starting this one. Defaults to false."
                ),
            },
        },
        "required": [],
    },
}


# ---------------------------------------------------------------------------
# vdotool_start_screenshare
# ---------------------------------------------------------------------------

START_SCREENSHARE = {
    "name": "vdotool_start_screenshare",
    "description": (
        "Start a new vdocall where the remote device publishes its "
        "SCREEN instead of its camera. Uses the same bidirectional "
        "machinery as vdotool_start (the agent can speak via "
        "vdotool_say; the mic is still on so the user can talk back; "
        "frames of the shared screen stream back through the watcher). "
        "Only one vdocall can be active at a time; pass force=true to "
        "replace an existing one.\n\n"
        "Use this when seeing the user's screen is more useful than "
        "seeing their room: debugging an app, pair-programming, "
        "walking through a spreadsheet, reviewing a design tool, "
        "showing a setting buried in a menu, etc.\n\n"
        "Platform limitation (IMPORTANT): the Web API used for screen "
        "sharing (getDisplayMedia) is NOT available on iOS Safari. "
        "iPhone / iPad users MUST either open the push_link on a "
        "laptop/desktop browser or on Android Chrome. A usable "
        "workaround for iPhone users: AirPlay the phone's display to a "
        "Mac, then open the push_link on that Mac and share its "
        "screen. When you hand the push_link to the user, explicitly "
        "mention this limitation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": (
                    "Short human-readable title for this session "
                    "(e.g. 'Debugging React error', 'Excel formulas', "
                    "'IDE pair-programming'). Surfaced to the user and "
                    "logged."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "Optional longer note about what this session is "
                    "for. Shown to the agent on every turn via the "
                    "pre_llm_call hook so you stay oriented."
                ),
            },
            "screen_hint": {
                "type": "string",
                "description": (
                    "Optional hint for the user about WHICH screen / "
                    "window / tab to share (e.g. 'pick the VS Code "
                    "window', 'share your browser tab with the app', "
                    "'share your whole desktop')."
                ),
            },
            "force": {
                "type": "boolean",
                "description": (
                    "If true, end any currently-active vdocall before "
                    "starting this one. Defaults to false."
                ),
            },
        },
        "required": [],
    },
}


# ---------------------------------------------------------------------------
# vdotool_get_latest_frame
# ---------------------------------------------------------------------------

GET_LATEST_FRAME = {
    "name": "vdotool_get_latest_frame",
    "description": (
        "Return the most recent captured frame from the active "
        "vdocall as base64-encoded image data, together with metadata "
        "(age, quality classification). Use this when you need to "
        "SEE the user's environment on-demand, outside the normal "
        "auto-injection cadence. If the latest frame is older than "
        "30s or classified as 'blank', the response tells you so "
        "rather than making you hallucinate contents."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


# ---------------------------------------------------------------------------
# vdotool_end
# ---------------------------------------------------------------------------

END = {
    "name": "vdotool_end",
    "description": (
        "End the active vdocall. Stops the headless viewer, stops the "
        "background watcher, stops the TTS queue, and purges frames "
        "and audio for this session. Call when the user is done, when "
        "they explicitly ask to stop, or when they've gone silent for "
        "a long time (~3 minutes of blank/stale frames with no chat)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "Optional free-form note about the session close "
                    "(saved in the return payload, not stored "
                    "elsewhere)."
                ),
            },
            "keep_frames": {
                "type": "boolean",
                "description": (
                    "If true, leave the frames directory on disk for "
                    "inspection. Default false (purged)."
                ),
            },
        },
        "required": [],
    },
}


# ---------------------------------------------------------------------------
# vdotool_status
# ---------------------------------------------------------------------------

STATUS = {
    "name": "vdotool_status",
    "description": (
        "Return the active vdocall's state without pulling image "
        "bytes. Includes title, latest-frame age, viewer health, "
        "stack health, listener (mic) health, and voice config "
        "(TTS/STT providers + whether their API keys are set)."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


# ---------------------------------------------------------------------------
# vdotool_spawn_viewer
# ---------------------------------------------------------------------------

SPAWN_VIEWER = {
    "name": "vdotool_spawn_viewer",
    "description": (
        "Spawn (or restart) the headless Chromium viewer on the host "
        "that drives frame capture + TTS playback + mic recording for "
        "the active vdocall. vdotool_start auto-spawns it; call this "
        "tool only if the viewer crashed or capture stalled."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "replace": {
                "type": "boolean",
                "description": "Terminate any existing viewer first. Default true.",
                "default": True,
            },
        },
        "required": [],
    },
}


# ---------------------------------------------------------------------------
# vdotool_start_watcher
# ---------------------------------------------------------------------------

START_WATCHER = {
    "name": "vdotool_start_watcher",
    "description": (
        "Start (or restart) the background frame watcher for the "
        "active vdocall. The watcher is a daemon thread that polls "
        "the frames directory and pushes '[vdotool auto]' messages "
        "into the chat as new frames arrive. Auto-started by "
        "vdotool_start; call this only to recover a dead watcher."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


# ---------------------------------------------------------------------------
# vdotool_say
# ---------------------------------------------------------------------------

SAY = {
    "name": "vdotool_say",
    "description": (
        "Speak text out loud on the user's phone. The plugin "
        "synthesizes the text to an MP3 via whichever TTS provider "
        "the user configured in ~/.hermes/config.yaml (Edge, xAI, "
        "ElevenLabs, OpenAI, Gemini, Piper, etc.), queues it for the "
        "viewer Chrome to stream into the VDO.Ninja room, and the "
        "phone — already a peer in the room — plays it through its "
        "speaker.\n\n"
        "Before calling this, check voice_config.tts.ready (surfaced "
        "in vdotool_start and vdotool_status). If false, don't call "
        "this tool — it will return tts_failed.\n\n"
        "Keep each utterance short (1-2 sentences, ≤ ~150 chars). "
        "Pass interrupt=true for urgent warnings (flushes the queue "
        "and aborts the current clip).\n\n"
        "FIFO queue semantics: consecutive calls queue up and play "
        "in order. The plugin enforces a 60-second rolling character "
        "cap to prevent runaway synthesis."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Text to speak. Truncated to VDOTOOL_TTS_MAX_CHARS (default 800).",
            },
            "interrupt": {
                "type": "boolean",
                "description": (
                    "Clear the pending queue + abort current clip "
                    "before queuing this one. Default false."
                ),
                "default": False,
            },
        },
        "required": ["text"],
    },
}
