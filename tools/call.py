"""Outbound call tools: ``clawtalk_call`` and ``clawtalk_call_status``."""

from __future__ import annotations

import logging
from typing import Any

from ..formatting import format_phone_number
from .common import ok, require, runtime, tool

__all__ = ["CALL_SCHEMA", "CALL_STATUS_SCHEMA", "clawtalk_call", "clawtalk_call_status"]

logger = logging.getLogger(__name__)

CALL_SCHEMA = {
    "name": "clawtalk_call",
    "description": (
        "Place an outbound phone call through ClawTalk. The call connects to "
        "the ClawTalk voice AI, which holds a real conversation with the "
        "person who answers. Use when the user asks to call, phone, or ring "
        "someone. Returns a call_id for clawtalk_call_status."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": "Phone number to call in E.164 format, e.g. +353851234567",
            },
            "greeting": {
                "type": "string",
                "description": "Line spoken the moment the call connects",
            },
            "purpose": {
                "type": "string",
                "description": "Why the call is being made; used for context and the outcome report",
            },
        },
        "required": ["to"],
    },
}

CALL_STATUS_SCHEMA = {
    "name": "clawtalk_call_status",
    "description": (
        "Check whether a ClawTalk call is still ringing, connected, or over - "
        'or hang it up with action "end". Takes the call_id returned by '
        "clawtalk_call."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "call_id": {
                "type": "string",
                "description": "Call ID returned by clawtalk_call",
            },
            "action": {
                "type": "string",
                "enum": ["status", "end"],
                "description": 'What to do: "status" (default) reads state, "end" hangs up',
            },
        },
        "required": ["call_id"],
    },
}


@tool("clawtalk_call")
def clawtalk_call(args: dict[str, Any], **_kwargs: Any) -> str:
    (to,) = require(args, "to")
    logger.info("[clawtalk] initiating call to %s", to)

    result = runtime().client.calls.initiate(
        to=to,
        greeting=args.get("greeting"),
        purpose=args.get("purpose"),
    )

    call_id = result.get("call_id", "")
    pretty = format_phone_number(to)
    return ok(
        {
            "call_id": call_id,
            "status": result.get("status") or "initiating",
            "to": pretty,
        },
        f"Call initiated to {pretty}. Call ID: {call_id}",
    )


@tool("clawtalk_call_status")
def clawtalk_call_status(args: dict[str, Any], **_kwargs: Any) -> str:
    (call_id,) = require(args, "call_id")
    action = args.get("action") or "status"
    client = runtime().client

    if action == "end":
        logger.info("[clawtalk] ending call %s", call_id)
        client.calls.end(call_id)
        return ok({"call_id": call_id, "status": "ended"}, f"Call {call_id} ended.")

    logger.info("[clawtalk] checking status of call %s", call_id)
    result = client.calls.status(call_id)
    status = result.get("status") or "unknown"
    duration = result.get("duration_seconds")

    suffix = f" ({duration}s)" if duration else ""
    return ok(
        {"call_id": call_id, "status": status, "duration_seconds": duration},
        f"Call {call_id}: {status}{suffix}",
    )
