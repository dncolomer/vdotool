"""Voice-config introspection for vdotool.

The plugin is deliberately TTS/STT-provider-agnostic: it just calls
Hermes' ``text_to_speech_tool`` and ``transcribe_audio``. Whichever
provider is configured in ``~/.hermes/config.yaml`` is what actually
runs. This module lets the plugin (and therefore the agent) describe
that configuration to the user, so when voice doesn't work or hasn't
been set up, the agent can explain in plain English what to change.

Returned structure from ``get_voice_config_report()``:

    {
      "tts": {
        "provider": "xai",
        "is_default": False,
        "enabled_by_plugin": True,   # VDOTOOL_TTS_ENABLED
        "needs_key": ["XAI_API_KEY"],
        "key_present": False,
        "free": False,
        "local": False,
        "ready": False,
        "notes": "...",
      },
      "stt": { ... same shape ... },
      "overall_ready": bool,
      "suggestion_for_user": str,
    }
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_TTS_PROVIDER_KEYS: dict[str, tuple[str, ...] | None] = {
    "edge":       None,
    "piper":      None,
    "kittentts":  None,
    "neutts":     None,
    "elevenlabs": ("ELEVENLABS_API_KEY",),
    "openai":     ("VOICE_TOOLS_OPENAI_KEY", "OPENAI_API_KEY"),
    "minimax":    ("MINIMAX_API_KEY",),
    "mistral":    ("MISTRAL_API_KEY",),
    "gemini":     ("GEMINI_API_KEY",),
    "xai":        ("XAI_API_KEY",),
}

_STT_PROVIDER_KEYS: dict[str, tuple[str, ...] | None] = {
    "local":         None,
    "local_command": None,
    "groq":          ("GROQ_API_KEY",),
    "openai":        ("VOICE_TOOLS_OPENAI_KEY", "OPENAI_API_KEY"),
    "mistral":       ("MISTRAL_API_KEY",),
    "xai":           ("XAI_API_KEY",),
}

_LOCAL_PROVIDERS = {"edge", "piper", "kittentts", "neutts", "local", "local_command"}
_FREE_PROVIDERS = _LOCAL_PROVIDERS | {"edge"}

_TTS_DEFAULT = "edge"
_STT_DEFAULT = "local"


def _read_hermes_dotenv() -> dict[str, str]:
    try:
        env_path = Path(os.environ.get("HOME", "~")).expanduser() / ".hermes" / ".env"
        if not env_path.is_file():
            return {}
        out: dict[str, str] = {}
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k:
                out[k] = v
        return out
    except OSError:
        return {}


def _is_env_var_set(var: str, dotenv: dict[str, str] | None = None) -> bool:
    if os.environ.get(var):
        return True
    if dotenv and dotenv.get(var):
        return True
    return False


def _load_hermes_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config  # type: ignore
        cfg = load_config()
        if isinstance(cfg, dict):
            return cfg
    except Exception:  # noqa: BLE001
        pass

    try:
        yaml_path = Path(os.environ.get("HOME", "~")).expanduser() / ".hermes" / "config.yaml"
        if not yaml_path.is_file():
            return {}
        import yaml  # type: ignore
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _assess_provider(
    kind: str,
    provider_map: dict[str, tuple[str, ...] | None],
    configured: str | None,
    default: str,
    dotenv: dict[str, str],
    plugin_enabled: bool,
) -> dict[str, Any]:
    is_default = configured is None or not str(configured).strip()
    provider = (configured or default).lower().strip()

    keys = provider_map.get(provider)
    needs_key_list: list[str] = list(keys) if keys else []
    if needs_key_list:
        key_present = any(_is_env_var_set(k, dotenv) for k in needs_key_list)
    else:
        key_present = True

    is_local = provider in _LOCAL_PROVIDERS
    is_free = provider in _FREE_PROVIDERS
    unknown_provider = provider not in provider_map
    ready = plugin_enabled and not unknown_provider and key_present

    if not plugin_enabled:
        env_var = "VDOTOOL_TTS_ENABLED" if kind == "tts" else "VDOTOOL_STT_ENABLED"
        if kind == "tts":
            notes = (
                f"Plugin-level switch {env_var} is off; vdotool_say will "
                f"return a `tts_disabled` error regardless of Hermes "
                f"config. Re-enable with export {env_var}=1."
            )
        else:
            notes = (
                f"Plugin-level switch {env_var} is off; the phone's mic "
                f"utterances won't be transcribed. Re-enable with "
                f"export {env_var}=1."
            )
    elif unknown_provider:
        notes = (
            f"Hermes {kind} provider is set to '{provider}' but the "
            "vdotool plugin doesn't have key metadata for it. Voice may "
            "or may not work — the plugin will just call Hermes' own "
            "wrapper and trust whatever happens."
        )
    elif not keys:
        if is_default:
            notes = (
                f"Using Hermes' default {kind} provider '{provider}' "
                "(free, no API key needed). Voice will work out of the box."
            )
        else:
            notes = (
                f"Using {kind} provider '{provider}' (free, no API key). "
                "Voice will work out of the box."
            )
    else:
        key_summary = " or ".join(needs_key_list)
        if key_present:
            notes = (
                f"Using {kind} provider '{provider}'. Required key "
                f"({key_summary}) is present; voice should work."
            )
        else:
            fallback = _TTS_DEFAULT if kind == "tts" else _STT_DEFAULT
            notes = (
                f"Hermes {kind} provider is '{provider}' which needs "
                f"{key_summary} exported (or set in ~/.hermes/.env). "
                "Currently NOT set — voice will fail at call time. "
                "Either export the key, or switch the provider to a "
                f"free one like '{fallback}' in ~/.hermes/config.yaml."
            )

    return {
        "provider": provider,
        "is_default": is_default,
        "enabled_by_plugin": plugin_enabled,
        "needs_key": needs_key_list or None,
        "key_present": key_present,
        "free": is_free,
        "local": is_local,
        "unknown_provider": unknown_provider,
        "ready": ready,
        "notes": notes,
    }


def get_voice_config_report() -> dict[str, Any]:
    cfg = _load_hermes_config()
    dotenv = _read_hermes_dotenv()

    tts_cfg = cfg.get("tts", {}) if isinstance(cfg, dict) else {}
    stt_cfg = cfg.get("stt", {}) if isinstance(cfg, dict) else {}

    tts_enabled = os.environ.get("VDOTOOL_TTS_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )
    stt_enabled = os.environ.get("VDOTOOL_STT_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )

    tts = _assess_provider(
        kind="tts",
        provider_map=_TTS_PROVIDER_KEYS,
        configured=(tts_cfg.get("provider") if isinstance(tts_cfg, dict) else None),
        default=_TTS_DEFAULT,
        dotenv=dotenv,
        plugin_enabled=tts_enabled,
    )
    stt = _assess_provider(
        kind="stt",
        provider_map=_STT_PROVIDER_KEYS,
        configured=(stt_cfg.get("provider") if isinstance(stt_cfg, dict) else None),
        default=_STT_DEFAULT,
        dotenv=dotenv,
        plugin_enabled=stt_enabled,
    )

    overall_ready = bool(tts.get("ready") and stt.get("ready"))

    if overall_ready:
        suggestion = (
            f"Voice is available both ways: I can speak to your phone via {tts['provider']}"
            f" and hear you via {stt['provider']}. If you'd rather stay silent, "
            "just type as usual — voice is a bonus, not required."
        )
    elif tts.get("ready") and not stt.get("ready"):
        suggestion = (
            f"I can speak to your phone ({tts['provider']}), but I can't hear you — "
            f"{stt.get('notes')}"
        )
    elif stt.get("ready") and not tts.get("ready"):
        suggestion = (
            f"I can hear you ({stt['provider']}), but I can't speak — "
            f"{tts.get('notes')}"
        )
    else:
        suggestion = (
            "Voice (TTS + STT) is not set up right now, so we'll stick to the chat "
            "terminal for this session. "
            + ("To add voice: " + tts.get("notes", "") if tts.get("notes") else "")
            + " "
            + ("And: " + stt.get("notes", "") if stt.get("notes") else "")
        ).strip()

    return {
        "tts": tts,
        "stt": stt,
        "overall_ready": overall_ready,
        "suggestion_for_user": suggestion,
    }


get_voice_status = get_voice_config_report
