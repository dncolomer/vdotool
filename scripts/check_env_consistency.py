#!/usr/bin/env python3
"""Check that every ``VDOTOOL_*`` env var referenced in source is
also documented in plugin.yaml.

The goal is to prevent silent drift: it's easy to add a new env var in
``tools.py`` and forget to list it in ``plugin.yaml``'s ``requires_env``
section (or in the README table). This script fails the lint if that
happens.

Exit 0 if code and docs agree; exit 1 with a diff on drift.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent

# Where to look for os.environ references.
SOURCE_GLOBS = [
    "*.py",
    "scripts/*.py",
    "vdo_ninja/vdotool/*.py",
]

ENV_VAR_RE = re.compile(r"\bVDOTOOL_[A-Z0-9_]+\b")

# Env vars that are intentionally NOT surfaced in plugin.yaml (e.g.
# internal test knobs). Keep this list short.
INTERNAL_ENV_VARS: set[str] = set()


def find_code_env_vars() -> set[str]:
    found: set[str] = set()
    for pattern in SOURCE_GLOBS:
        for path in REPO.glob(pattern):
            try:
                text = path.read_text()
            except OSError:
                continue
            for m in ENV_VAR_RE.finditer(text):
                found.add(m.group(0))
    return found


def find_yaml_env_vars() -> set[str]:
    yml = REPO / "plugin.yaml"
    if not yml.is_file():
        return set()
    text = yml.read_text()
    # Look for "- name: VDOTOOL_..." lines.
    pattern = re.compile(r"-\s*name:\s*(VDOTOOL_[A-Z0-9_]+)")
    return set(pattern.findall(text))


def main() -> int:
    code = find_code_env_vars() - INTERNAL_ENV_VARS
    yaml = find_yaml_env_vars()

    missing_in_yaml = sorted(code - yaml)
    stale_in_yaml = sorted(yaml - code)

    ok = True
    if missing_in_yaml:
        ok = False
        print("env vars referenced in code but NOT documented in plugin.yaml:")
        for v in missing_in_yaml:
            print(f"  - {v}")
    if stale_in_yaml:
        ok = False
        print("env vars documented in plugin.yaml but NOT referenced in code:")
        for v in stale_in_yaml:
            print(f"  - {v}")
    if ok:
        print(f"OK: {len(code)} env vars consistent across code and plugin.yaml")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
