"""``clawtalk_status`` - connection, version, account, and health at a glance."""

from __future__ import annotations

import logging
from typing import Any

from ..version import __version__
from .common import ok, runtime, tool

__all__ = ["STATUS_SCHEMA", "clawtalk_status"]

logger = logging.getLogger(__name__)

STATUS_SCHEMA = {
    "name": "clawtalk_status",
    "description": (
        "Report ClawTalk connection health: whether the event socket is live, "
        "which server and plugin version are in use, the authenticated "
        'account, and the phone number. Set "full": true to also run the '
        "server-side doctor checks. Use when the user asks whether ClawTalk is "
        "working, or when calls and texts are not arriving."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "full": {
                "type": "boolean",
                "description": "Also run the server-side doctor checks (slower)",
            }
        },
        "required": [],
    },
}


@tool("clawtalk_status")
def clawtalk_status(args: dict[str, Any], **_kwargs: Any) -> str:
    logger.info("[clawtalk] checking status")
    rt = runtime()
    ws = rt.ws

    payload: dict[str, Any] = {
        "connected": rt.ws_connected,
        "server": rt.config.server,
        "version": __version__,
        "websocket_state": "open" if rt.ws_connected else "closed",
    }

    if ws is not None:
        last_ping = ws.last_ping
        last_pong = ws.last_pong
        payload["last_ping_at"] = last_ping.isoformat() if last_ping else None
        payload["last_pong_at"] = last_pong.isoformat() if last_pong else None
        if ws.fatal_error:
            payload["fatal_error"] = ws.fatal_error
    else:
        # Tools also run in `hermes chat`, where no gateway owns this process.
        payload["websocket_state"] = "not_running_in_this_process"

    # Best-effort: a bad key must still leave the rest of the report readable.
    try:
        me = rt.client.user.me()
        payload["user"] = me.get("email") or me.get("user_id")
        payload["plan"] = me.get("effective_tier") or me.get("subscription_tier")
        payload["phone_number"] = me.get("dedicated_number") or me.get("system_number")
    except Exception as exc:  # noqa: BLE001 - degraded, not failed
        payload["account_error"] = str(exc)

    if args.get("full"):
        payload["doctor"] = rt.doctor.run_all().to_dict()

    parts = [
        f"WebSocket: {payload['websocket_state']}",
        f"Server: {payload['server']}",
        f"Version: {payload['version']}",
    ]
    if payload.get("user"):
        parts.append(f"User: {payload['user']}")
    if payload.get("phone_number"):
        parts.append(f"Number: {payload['phone_number']}")

    return ok(payload, ". ".join(parts))
