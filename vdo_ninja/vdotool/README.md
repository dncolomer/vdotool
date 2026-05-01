# vdotool — browser-side additions to the VDO.Ninja fork

This subdirectory is the only thing that was added to the upstream
VDO.Ninja fork for the vdotool integration. The rest of the fork is
completely untouched except for a single opt-in `<script>` block near
the end of `../index.html` that loads these scripts when the URL
contains `?vdotool=1&view=1&sessionId=<id>`. Without those query
params, VDO.Ninja behaves exactly like upstream.

## Files

| File | Role |
|------|------|
| `capture.js` | Browser-side module. Snapshots the first playing remote `<video>` every 3s to JPEG and POSTs it to `/vdotool/frame`. |
| `speaker.js` | Spawns a hidden companion pusher iframe in the same VDO.Ninja room, installs a `getUserMedia` override so it publishes the agent's TTS as its "microphone", polls `/vdotool/audio-queue`, and plays queued clips into the room. |
| `listener.js` | VAD on the incoming remote audio track (i.e. the phone's mic). Records each utterance with `MediaRecorder` (WebM/Opus) and POSTs it to `/vdotool/audio-in`. Respects a mute window (`/vdotool/listener-mute`) so it doesn't feed the agent's own TTS back. |
| `writer.py` | Tiny stdlib HTTP sidecar. Accepts frame + audio POSTs; serves TTS clips; reports listener mute window and listener status. All per-session directories must be created by the Hermes plugin first; the writer refuses to create them. |
| `start-capture.sh` | Launcher used by systemd or any process manager. |

## Filesystem contract

The writer and the Hermes plugin agree on this layout and nothing else:

```
$VDOTOOL_FRAMES_DIR/
└── <session_id>/                   # created by the plugin (vdotool_start)
    ├── frame-<unix_ms>.jpg
    ├── latest.jpg                  # symlink, newest frame
    ├── audio_out/
    │   ├── queue.json              # pending/played/interrupt_epoch
    │   ├── queue.json.lock         # fcntl advisory lock
    │   ├── muted_until_ms.txt      # echo-suppression deadline
    │   └── <unix_ms>.mp3           # TTS clip bytes
    └── audio_in/
        ├── utterance-<unix_ms>.webm
        ├── utterance-<unix_ms>.webm.processed   # marker
        └── .listener_status.json
```

The **plugin** creates `<session_id>/` at session start; the **writer**
refuses to write into directories it didn't create. This is both a
rate-limiter against stray traffic and a cleanup guarantee.

## Deploy sketch (nginx)

```
server {
    listen 443 ssl;
    server_name vdotool.example.com;
    root /srv/vdotool/vdo_ninja;
    index index.html;

    location = /vdotool/frame {
        proxy_pass http://127.0.0.1:8765;
        client_max_body_size 8m;
        proxy_request_buffering off;
    }
    location = /vdotool/audio-ack     { proxy_pass http://127.0.0.1:8765; }
    location = /vdotool/audio-queue   { proxy_pass http://127.0.0.1:8765; }
    location = /vdotool/audio-in      { proxy_pass http://127.0.0.1:8765; client_max_body_size 12m; }
    location = /vdotool/listener-mute   { proxy_pass http://127.0.0.1:8765; }
    location = /vdotool/listener-status { proxy_pass http://127.0.0.1:8765; }
    location   /vdotool/audio/ { proxy_pass http://127.0.0.1:8765; }
    location   /vdotool/healthz { proxy_pass http://127.0.0.1:8765; }

    location / {
        try_files $uri $uri/ =404;
    }
}
```

## Systemd unit (illustrative)

```
[Unit]
Description=vdotool frame writer
After=network.target

[Service]
Environment=VDOTOOL_FRAMES_DIR=/var/lib/vdotool/frames
Environment=VDOTOOL_WRITER_HOST=127.0.0.1
Environment=VDOTOOL_WRITER_PORT=8765
ExecStart=/srv/vdotool/vdo_ninja/vdotool/start-capture.sh
Restart=on-failure
User=vdotool

[Install]
WantedBy=multi-user.target
```

## License

Everything in this subdirectory is AGPL-3.0 to match the upstream
VDO.Ninja fork it patches.
