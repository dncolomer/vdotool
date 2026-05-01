#!/usr/bin/env bash
# Minimal JS syntax check for vdotool browser scripts.
#
# We don't run a full bundler or eslint; a plain `node --check` catches
# ~90% of regressions (typos, stray commas, mismatched braces) without
# adding deps. If you have eslint globally installed and want deeper
# checks, run `eslint` over vdo_ninja/vdotool/*.js manually.
#
# Exit 0 if all files parse; exit 1 on the first failure.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JS_FILES=(
    "$REPO_DIR/vdo_ninja/vdotool/capture.js"
    "$REPO_DIR/vdo_ninja/vdotool/speaker.js"
    "$REPO_DIR/vdo_ninja/vdotool/listener.js"
)

if ! command -v node >/dev/null 2>&1; then
    echo "node not found on PATH; skipping JS lint." >&2
    exit 0
fi

FAIL=0
for f in "${JS_FILES[@]}"; do
    if [ ! -f "$f" ]; then
        echo "MISSING $f"
        FAIL=1
        continue
    fi
    if node --check "$f" 2>&1; then
        echo "OK     $(basename "$f")"
    else
        echo "FAIL   $(basename "$f")"
        FAIL=1
    fi
done

exit "$FAIL"
