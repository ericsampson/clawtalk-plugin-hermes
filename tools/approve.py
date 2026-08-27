"""``clawtalk_approve`` - ask the user to approve an action from their phone."""

from __future__ import annotations

import logging
from typing import Any

from .common import ok, require, runtime, tool

__all__ = ["APPROVE_SCHEMA", "clawtalk_approve"]

logger = logging.getLogger(__name__)

APPROVE_SCHEMA = {
    "name": "clawtalk_approve",
    "description": (
        "Ask the user to approve an action by push notification to their "
        "phone, and wait for the answer. Use before anything sensitive or "
        "irreversible: deleting data, spending money, or sending a message on "
        "the user's behalf. Returns one of: approved, denied, timeout, "
        "no_devices, no_devices_reached. On a voice call, tell the caller you "
        "are sending the notification before calling this."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": 'Short description of what needs approving, e.g. "Send email to boss"',
            },
            "details": {
                "type": "string",
                "description": "Extra context shown in the notification body",
            },
            "biometric": {
                "type": "boolean",
                "description": (
                    "Require Face ID / fingerprint confirmation. Use for "
                    "financial or destructive actions. Default false."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": "Seconds to wait before giving up (default 60)",
            },
        },
        "required": ["action"],
    },
}

DECISION_MESSAGES = {
    "approved": "User approved the action.",
    "denied": "User denied the action.",
    "timeout": "Approval request timed out (no response from user).",
    "no_devices": "No devices registered for push notifications.",
    "no_devices_reached": "Could not reach any registered devices.",
}


@tool("clawtalk_approve")
def clawtalk_approve(args: dict[str, Any], **_kwargs: Any) -> str:
    (action,) = require(args, "action")
    logger.info("[clawtalk] requesting approval: %s", action)

    decision = runtime().approvals.request_approval(
        action,
        details=args.get("details"),
        biometric=bool(args.get("biometric")),
        timeout_s=args.get("timeout"),
    )

    return ok(
        {"decision": decision},
        DECISION_MESSAGES.get(decision, f"Approval result: {decision}"),
    )
