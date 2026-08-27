"""``clawtalk_bot_config`` - read/update the bot profile and browse voices."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from ..errors import ToolError
from .common import ok, runtime, tool

__all__ = ["BOT_CONFIG_SCHEMA", "clawtalk_bot_config"]

logger = logging.getLogger(__name__)

BOT_CONFIG_SCHEMA = {
    "name": "clawtalk_bot_config",
    "description": (
        "Read or change how the ClawTalk phone bot presents itself - its name, "
        "role, custom instructions, call greeting, and TTS voice - or browse "
        'the voice catalogue. Use action "get" to read the current profile, '
        '"update" to change fields, "list_voices" to find a voice ID before '
        "setting voice_preference. Changes take effect on the next call."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["get", "update", "list_voices"],
                "description": "What to do",
            },
            "agent_name": {"type": "string", "description": "Bot name, e.g. Daisy"},
            "bot_role": {
                "type": "string",
                "description": 'Bot role, e.g. "live phone voice for Smokies Motels"',
            },
            "custom_instructions": {
                "type": "string",
                "description": "Behaviour rules, business policies, pricing, and similar",
            },
            "greeting": {
                "type": "string",
                "description": "Line spoken when a call connects",
            },
            "voice_preference": {
                "type": "string",
                "description": "Voice ID from list_voices, e.g. Rime.ArcanaV3.astra",
            },
            "provider": {
                "type": "string",
                "description": (
                    'Voice provider for list_voices (default "rime"). One of: '
                    "rime, minimax, telnyx, inworld, resemble, aws, azure"
                ),
            },
            "language": {
                "type": "string",
                "description": 'Filter voices by language code, e.g. "en" or "fr-FR"',
            },
            "gender": {
                "type": "string",
                "description": 'Filter voices by gender ("Male" or "Female")',
            },
            "accent": {
                "type": "string",
                "description": 'Filter voices by accent, e.g. "British"',
            },
            "search": {
                "type": "string",
                "description": "Filter voices by name or label substring",
            },
        },
        "required": ["action"],
    },
}

PROFILE_FIELDS = (
    "agent_name",
    "bot_role",
    "custom_instructions",
    "greeting",
    "voice_preference",
)

CACHE_TTL_S = 300.0
MAX_VOICES_RETURNED = 20
MAX_LABEL_CHARS = 80
DEFAULT_PROVIDER = "rime"


@dataclass
class _CacheEntry:
    voices: list[dict[str, Any]]
    providers: list[str]
    default_voice: str
    fetched_at: float


# The catalogue runs to thousands of voices and changes rarely, so a short
# per-provider cache keeps repeated browsing cheap.
_voice_cache: dict[str, _CacheEntry] = {}
_cache_lock = threading.Lock()


@tool("clawtalk_bot_config")
def clawtalk_bot_config(args: dict[str, Any], **_kwargs: Any) -> str:
    action = args.get("action")
    if action == "get":
        return _handle_get()
    if action == "update":
        return _handle_update(args)
    if action == "list_voices":
        return _handle_list_voices(args)
    raise ToolError("clawtalk_bot_config", f"Unknown action: {action!r}")


def _profile(me: dict[str, Any]) -> dict[str, Any]:
    return {
        "agent_name": me.get("agent_name"),
        "display_name": me.get("display_name"),
        "bot_role": me.get("bot_role") or "personal AI assistant",
        "custom_instructions": me.get("custom_instructions"),
        "greeting": me.get("greeting"),
        "voice_preference": me.get("voice_preference"),
    }


def _handle_get() -> str:
    logger.info("[clawtalk] getting bot config")
    return ok(_profile(runtime().client.user.me()))


def _handle_update(args: dict[str, Any]) -> str:
    logger.info("[clawtalk] updating bot config")
    fields = {key: args[key] for key in PROFILE_FIELDS if args.get(key) is not None}
    if not fields:
        raise ToolError("clawtalk_bot_config", "No fields provided for update")

    client = runtime().client
    client.user.update_me(fields)
    return ok(
        _profile(client.user.me()),
        "Bot config updated. Changes take effect on the next call.",
    )


def _handle_list_voices(args: dict[str, Any]) -> str:
    provider = str(args.get("provider") or DEFAULT_PROVIDER)
    logger.info("[clawtalk] listing voices for provider: %s", provider)

    entry = _fetch_voices(provider)
    voices = _filter_voices(
        entry.voices,
        language=args.get("language"),
        gender=args.get("gender"),
        accent=args.get("accent"),
        search=args.get("search"),
    )

    capped = voices[:MAX_VOICES_RETURNED]
    return ok(
        {
            "default_voice": entry.default_voice,
            "provider": provider,
            "providers": entry.providers,
            "total_matching": len(voices),
            "showing": len(capped),
            "voices": [
                {
                    "id": v.get("id"),
                    "name": v.get("name"),
                    "provider": v.get("provider"),
                    "language": v.get("language"),
                    "gender": v.get("gender"),
                    "label": _truncate_label(v.get("label")),
                }
                for v in capped
            ],
        },
        f"{len(capped)} of {len(voices)} matching voice(s) from {provider}",
    )


def _fetch_voices(provider: str) -> _CacheEntry:
    now = time.monotonic()
    with _cache_lock:
        cached = _voice_cache.get(provider)
        if cached is not None and now - cached.fetched_at < CACHE_TTL_S:
            return cached

    result = runtime().client.voices.list(provider)
    entry = _CacheEntry(
        voices=list(result.get("voices") or []),
        providers=list(result.get("providers") or []),
        default_voice=str(result.get("default_voice") or ""),
        fetched_at=now,
    )
    with _cache_lock:
        _voice_cache[provider] = entry
    return entry


def _filter_voices(
    voices: list[dict[str, Any]],
    *,
    language: str | None,
    gender: str | None,
    accent: str | None,
    search: str | None,
) -> list[dict[str, Any]]:
    result = voices

    if language:
        needle = language.lower()
        result = [
            v for v in result if str(v.get("language") or "").lower().startswith(needle)
        ]
    if gender:
        needle = gender.lower()
        result = [v for v in result if str(v.get("gender") or "").lower() == needle]
    if accent:
        needle = accent.lower()
        result = [v for v in result if needle in str(v.get("accent") or "").lower()]
    if search:
        needle = search.lower()
        result = [
            v
            for v in result
            if needle in str(v.get("name") or "").lower()
            or needle in str(v.get("id") or "").lower()
            or needle in str(v.get("label") or "").lower()
        ]
    return result


def _truncate_label(label: str | None) -> str | None:
    if not label:
        return None
    return label if len(label) <= MAX_LABEL_CHARS else label[: MAX_LABEL_CHARS - 1] + "_"
