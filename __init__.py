"""ClawTalk for Hermes - voice, SMS, missions, and approvals on one plugin.

``register(ctx)`` wires up four surfaces:

* a **gateway platform** (``clawtalk``) whose adapter owns the outbound
  WebSocket to the ClawTalk server and turns every inbound call, text, and
  mission event into a normal Hermes agent turn;
* **21 agent tools** for placing calls, texting, running missions, and asking
  the user to approve something from their phone;
* the **``hermes clawtalk``** CLI namespace and the ``/clawtalk`` slash
  command; and
* a bundled **mission skill**.

Everything heavy - ``aiohttp``, ``gateway.platforms.base``, the adapter - is
imported lazily. Plugin discovery runs on *every* ``hermes`` invocation,
including ``hermes chat``, which never touches a gateway platform.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .version import __version__

__all__ = ["__version__", "register"]

logger = logging.getLogger(__name__)

PLATFORM_NAME = "clawtalk"
PLATFORM_LABEL = "ClawTalk"

PLATFORM_HINT = (
    "You are reachable by phone through ClawTalk. Voice turns are spoken "
    "aloud: keep them to one to three sentences with no markdown, lists, or "
    "URLs. SMS turns are plain text with a hard character cap. Use "
    "clawtalk_approve before anything sensitive or irreversible."
)


# -- platform plumbing -----------------------------------------------------


def _check_requirements() -> bool:
    """Passive dependency probe. Never installs anything."""
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return True


def _build_adapter(config: Any) -> Any:
    """Construct the adapter, importing gateway internals only now."""
    from .adapter import ClawTalkAdapter

    return ClawTalkAdapter(config)


def _is_connected(_config: Any) -> bool:
    """Whether ClawTalk is configured enough to count as connected."""
    from .config import load_config

    return bool(load_config().has_api_key)


def _env_enablement() -> dict[str, Any] | None:
    """Seed ``PlatformConfig.extra`` from env before the adapter is built.

    Without this, an env-only setup would not show up in
    ``hermes gateway status`` until the adapter had already been constructed.
    """
    api_key = os.getenv("CLAWTALK_API_KEY")
    if not api_key:
        return None

    extra: dict[str, Any] = {"api_key": api_key}
    for env_name, key in (
        ("CLAWTALK_SERVER", "server"),
        ("CLAWTALK_OWNER_NAME", "owner_name"),
        ("CLAWTALK_AGENT_NAME", "agent_name"),
        ("CLAWTALK_GREETING", "greeting"),
    ):
        value = os.getenv(env_name)
        if value:
            extra[key] = value
    return extra


def _parse_target_ref(target_ref: str):
    """Accept ``sms:+15551234567`` and friends as explicit send targets.

    Returns ``(chat_id, thread_id)`` or ``None`` to fall through to the
    channel directory.
    """
    if not isinstance(target_ref, str):
        return None
    for prefix in ("sms:", "call:", "walkie:", "mission:"):
        if target_ref.startswith(prefix):
            return (target_ref, None)
    return None


async def _standalone_send(
    _pconfig: Any,
    chat_id: str,
    message: str,
    *,
    thread_id: str | None = None,
    media_files: Any = None,
    force_document: bool = False,
) -> dict[str, Any]:
    """Out-of-process delivery for cron jobs that outlive the gateway.

    Only SMS is deliverable this way. Voice and walkie replies must ride the
    live WebSocket, which a standalone process does not have.
    """
    import asyncio

    from .formatting import truncate
    from .runtime import get_runtime

    if not chat_id.startswith("sms:"):
        return {
            "error": (
                f"ClawTalk can only deliver SMS out-of-process; "
                f"'{chat_id}' needs a running gateway."
            )
        }

    target = chat_id[len("sms:") :]
    if not target.startswith("+"):
        target = f"+{target}"

    runtime = get_runtime()
    body = truncate(message.strip(), runtime.config.sms_max_reply_chars)

    try:
        result = await asyncio.to_thread(
            runtime.client.sms.send, to=target, message=body
        )
    except Exception as exc:  # noqa: BLE001 - contract is a dict, not a raise
        return {"error": f"ClawTalk SMS send failed: {exc}"}

    return {
        "success": True,
        "platform": PLATFORM_NAME,
        "chat_id": chat_id,
        "message_id": str(result.get("id") or ""),
    }


# -- entry point -----------------------------------------------------------


def register(ctx: Any) -> None:
    """Plugin entry point, called once by the Hermes plugin system."""
    from .config import load_config
    from .runtime import set_plugin_context

    set_plugin_context(ctx)
    config = load_config(ctx=ctx)

    if not config.has_api_key:
        logger.warning(
            "[clawtalk] loaded without an API key - tools will fail until "
            "CLAWTALK_API_KEY (or gateway.platforms.clawtalk.extra.api_key) is set"
        )

    _register_platform(ctx)
    _register_tools(ctx, config)
    _register_cli(ctx)
    _register_command(ctx)
    _register_skills(ctx)

    logger.info(
        "[clawtalk] plugin loaded (v%s, server: %s)", __version__, config.server
    )


def _register_platform(ctx: Any) -> None:
    ctx.register_platform(
        name=PLATFORM_NAME,
        label=PLATFORM_LABEL,
        adapter_factory=_build_adapter,
        check_fn=_check_requirements,
        is_connected=_is_connected,
        required_env=["CLAWTALK_API_KEY"],
        install_hint="pip install aiohttp",
        allowed_users_env="CLAWTALK_ALLOWED_USERS",
        allow_all_env="CLAWTALK_ALLOW_ALL_USERS",
        cron_deliver_env_var="CLAWTALK_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        env_enablement_fn=_env_enablement,
        parse_target_ref_fn=_parse_target_ref,
        platform_hint=PLATFORM_HINT,
        pii_safe=True,
        emoji="📞",
        allow_update_command=False,
    )


def _register_tools(ctx: Any, config: Any) -> None:
    from .tools import register_tools

    count = register_tools(ctx, include_missions=config.missions.enabled)
    logger.info("[clawtalk] registered %d agent tools", count)


def _register_cli(ctx: Any) -> None:
    from .cli import handle_clawtalk_cli, setup_clawtalk_cli

    ctx.register_cli_command(
        name="clawtalk",
        help="ClawTalk plugin utilities (doctor, logs, status)",
        setup_fn=setup_clawtalk_cli,
        handler_fn=handle_clawtalk_cli,
    )


def _register_command(ctx: Any) -> None:
    from .commands import ARGS_HINT, handle_clawtalk_command

    ctx.register_command(
        "clawtalk",
        handler=handle_clawtalk_command,
        description="Show ClawTalk connection status, local checks, or active missions",
        args_hint=ARGS_HINT,
    )


def _register_skills(ctx: Any) -> None:
    """Expose bundled skills as ``clawtalk:<name>``."""
    skills_dir = Path(__file__).parent / "skills"
    if not skills_dir.is_dir():
        return

    for child in sorted(skills_dir.iterdir()):
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.exists():
            try:
                ctx.register_skill(child.name, skill_md)
            except Exception as exc:  # noqa: BLE001 - a bad skill must not
                logger.warning(                      # take the plugin down
                    "[clawtalk] could not register skill %s: %s", child.name, exc
                )
