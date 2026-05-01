#!/usr/bin/env bash
# vdotool frame writer launcher (systemd-friendly).
#
# Required environment:
#   VDOTOOL_FRAMES_DIR      Root directory for per-session dirs.
#
# Optional environment:
#   VDOTOOL_WRITER_HOST     Default 127.0.0.1.
#   VDOTOOL_WRITER_PORT     Default 8765.
#   VDOTOOL_KEEP_FRAMES_MIN Default 10 minutes.
#   VDOTOOL_MAX_FRAME_BYTES Default 4 MB.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -z "${VDOTOOL_FRAMES_DIR:-}" ]; then
    export VDOTOOL_FRAMES_DIR="/var/lib/vdotool/frames"
fi

mkdir -p "$VDOTOOL_FRAMES_DIR"

exec python3 "$SCRIPT_DIR/writer.py"
