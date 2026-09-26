"""ClawTalk plugin configuration.

Resolution order (highest precedence first):

1. Environment variables — ``CLAWTALK_*``
2. ``gateway.platforms.clawtalk.extra.*`` in ``config.yaml``
   (reaches us as ``PlatformConfig.extra``)
3. ``plugins.entries.clawtalk.*`` in ``config.yaml`` (via ``ctx.get_config``)
4. Built-in defaults

This mirrors the OpenClaw plugin's ``resolveConfig()`` while following the
Hermes convention that env beats YAML.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

DEFAULT_SERVER = "https://clawdtalk.com"
DEFAULT_OWNER_NAME = "there"
DEFAULT_AGENT_NAME = "ClawTalk"
DEFAULT_MISSION_OBSERVER_INTERVAL_S = 300.0
DEFAULT_MISSION_COOLDOWN_S = 300.0

# Kept identical to the OpenClaw plugin so a user who overrode
# ``voiceContext`` there sees the same baseline here.
DEFAULT_VOICE_CONTEXT = (
    "You are a voice assistant. Keep responses concise and conversational. "
    "Do not use markdown, bullet points, or formatting — this will be spoken aloud. "
    "Avoid lists. Use short, natural sentences. "
    "If you need to convey multiple points, use conversational transitions."
)

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default



def _first_set(*values: Any) -> Any:
    """Return the first value that was actually configured.

    Spelled out rather than chained with ``or`` because a legitimate ``False``
    or `0` must beat a lower-precedence source; ``or`` silently skips both
    and falls through to the default.
    """
    for value in values:
        if value is not None and value != "":
            return value
    return None


@dataclass(frozen=True)
class ObserverConfig:
    """Mission observer tuning."""

    enabled: bool = True
    interval_s: float = DEFAULT_MISSION_OBSERVER_INTERVAL_S
    cooldown_s: float = DEFAULT_MISSION_COOLDOWN_S


@dataclass(frozen=True)
class MissionsConfig:
    """Mission subsystem configuration."""

    enabled: bool = True
    default_voice: str | None = None
    default_model: str | None = None
    observer: ObserverConfig = field(default_factory=ObserverConfig)


@dataclass(frozen=True)
class ClawTalkConfig:
    """Fully resolved plugin configuration."""

    api_key: str = ""
    server: str = DEFAULT_SERVER
    owner_name: str = DEFAULT_OWNER_NAME
    agent_name: str = DEFAULT_AGENT_NAME
    greeting: str = ""
    auto_connect: bool = True
    voice_context: str = DEFAULT_VOICE_CONTEXT
    sms_max_reply_chars: int = 300
    home_channel: str = ""
    missions: MissionsConfig = field(default_factory=MissionsConfig)
    # Local allowlist of E.164 senders. Empty means "trust ClawTalk's
    # server-side gating" (see ClawTalkAdapter.authorization_is_upstream).
    allowed_users: tuple[str, ...] = ()

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    @property
    def ws_url(self) -> str:
        base = self.server.rstrip("/")
        if base.startswith("https://"):
            return "wss://" + base[len("https://") :] + "/ws"
        if base.startswith("http://"):
            return "ws://" + base[len("http://") :] + "/ws"
        return base + "/ws"


def _plugin_cfg_reader(ctx: Any) -> Callable[[str, Any], Any]:
    """Return a ``get(key, default)`` over ``plugins.entries.clawtalk.*``."""
    getter = getattr(ctx, "get_config", None) if ctx is not None else None
    if not callable(getter):
        return lambda _key, default=None: default

    def _get(key: str, default: Any = None) -> Any:
        try:
            value = getter(key, default)
        # The getter belongs to the host, not to us: a config lookup that
        # raises must fall back to the default, never break plugin load.
        except Exception:  # noqa: BLE001
            return default
        return default if value is None else value

    return _get


def load_config(
    extra: Mapping[str, Any] | None = None,
    ctx: Any = None,
) -> ClawTalkConfig:
    """Resolve the effective configuration.

    ``extra`` is ``PlatformConfig.extra`` when called from the adapter, and
    ``None`` when called from a tool/CLI context that only has plugin config.
    """
    extra = dict(extra or {})
    plugin_cfg = _plugin_cfg_reader(ctx)

    def pick(env_name: str, extra_key: str, plugin_key: str, default: Any = None) -> Any:
        env_value = os.getenv(env_name)
        if env_value not in (None, ""):
            return env_value
        if extra.get(extra_key) not in (None, ""):
            return extra[extra_key]
        return plugin_cfg(plugin_key, default)

    api_key = str(pick("CLAWTALK_API_KEY", "api_key", "api_key", "") or "")
    server = str(pick("CLAWTALK_SERVER", "server", "server", DEFAULT_SERVER) or DEFAULT_SERVER)
    owner_name = str(
        pick("CLAWTALK_OWNER_NAME", "owner_name", "owner_name", DEFAULT_OWNER_NAME)
        or DEFAULT_OWNER_NAME
    )
    agent_name = str(
        pick("CLAWTALK_AGENT_NAME", "agent_name", "agent_name", DEFAULT_AGENT_NAME)
        or DEFAULT_AGENT_NAME
    )

    raw_greeting = pick("CLAWTALK_GREETING", "greeting", "greeting", None)
    greeting = (
        str(raw_greeting).replace("{ownerName}", owner_name)
        if raw_greeting
        else f"Hey {owner_name}, what's up?"
    )

    voice_context = str(
        pick("CLAWTALK_VOICE_CONTEXT", "voice_context", "voice_context", DEFAULT_VOICE_CONTEXT)
        or DEFAULT_VOICE_CONTEXT
    )

    auto_connect = _as_bool(
        pick("CLAWTALK_AUTO_CONNECT", "auto_connect", "auto_connect", None), True
    )

    sms_max = int(
        _as_float(pick("CLAWTALK_SMS_MAX_CHARS", "sms_max_reply_chars", "sms_max_reply_chars", 300), 300)
    )

    raw_home_channel = pick("CLAWTALK_HOME_CHANNEL", "home_channel", "home_channel", "")
    if isinstance(raw_home_channel, Mapping):
        raw_home_channel = raw_home_channel.get("chat_id", "")
    home_channel = str(raw_home_channel or "").strip()

    missions_extra = extra.get("missions") if isinstance(extra.get("missions"), Mapping) else {}
    observer_extra = (
        missions_extra.get("observer")
        if isinstance(missions_extra.get("observer"), Mapping)
        else {}
    )

    missions_enabled = _as_bool(
        _first_set(
            os.getenv("CLAWTALK_MISSIONS_ENABLED"),
            missions_extra.get("enabled"),
            plugin_cfg("missions.enabled", None),
        ),
        True,
    )
    observer_enabled = missions_enabled and _as_bool(
        _first_set(
            os.getenv("CLAWTALK_MISSION_OBSERVER_ENABLED"),
            observer_extra.get("enabled"),
            plugin_cfg("missions.observer.enabled", None),
        ),
        True,
    )
    interval_s = _as_float(
        _first_set(
            os.getenv("CLAWTALK_MISSION_OBSERVER_INTERVAL_S"),
            observer_extra.get("interval_s"),
            plugin_cfg("missions.observer.interval_s", None),
        ),
        DEFAULT_MISSION_OBSERVER_INTERVAL_S,
    )
    cooldown_s = _as_float(
        _first_set(
            os.getenv("CLAWTALK_MISSION_COOLDOWN_S"),
            observer_extra.get("cooldown_s"),
            plugin_cfg("missions.observer.cooldown_s", None),
        ),
        DEFAULT_MISSION_COOLDOWN_S,
    )

    default_voice = _first_set(
        missions_extra.get("default_voice"), plugin_cfg("missions.default_voice", None)
    )
    default_model = _first_set(
        missions_extra.get("default_model"), plugin_cfg("missions.default_model", None)
    )

    raw_allowed = pick("CLAWTALK_ALLOWED_USERS", "allowed_users", "allowed_users", None)
    if isinstance(raw_allowed, str):
        allowed = tuple(part.strip() for part in raw_allowed.split(",") if part.strip())
    elif isinstance(raw_allowed, list | tuple):
        allowed = tuple(str(part).strip() for part in raw_allowed if str(part).strip())
    else:
        allowed = ()

    return ClawTalkConfig(
        api_key=api_key,
        server=server.rstrip("/"),
        owner_name=owner_name,
        agent_name=agent_name,
        greeting=greeting,
        auto_connect=auto_connect,
        voice_context=voice_context,
        sms_max_reply_chars=max(1, sms_max),
        home_channel=home_channel,
        missions=MissionsConfig(
            enabled=missions_enabled,
            default_voice=str(default_voice) if default_voice else None,
            default_model=str(default_model) if default_model else None,
            observer=ObserverConfig(
                enabled=observer_enabled,
                interval_s=max(10.0, interval_s),
                cooldown_s=max(0.0, cooldown_s),
            ),
        ),
        allowed_users=allowed,
    )
