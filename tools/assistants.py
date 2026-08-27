"""``clawtalk_assistants`` and ``clawtalk_insights``.

Assistant CRUD outside a mission, plus Telnyx conversation insights.
"""

from __future__ import annotations

import logging
from typing import Any

from ..errors import ToolError
from .common import ok, optional_json, require, runtime, tool

__all__ = [
    "ASSISTANTS_SCHEMA",
    "INSIGHTS_SCHEMA",
    "clawtalk_assistants",
    "clawtalk_insights",
]

logger = logging.getLogger(__name__)

ASSISTANTS_SCHEMA = {
    "name": "clawtalk_assistants",
    "description": (
        "List, inspect, create, or update ClawTalk voice assistants outside a "
        "mission. An assistant is a configured Telnyx voice AI with its own "
        "instructions, voice, model, and phone number. For mission work use "
        "clawtalk_mission_setup_agent instead, which also links the assistant "
        "to the run."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "get", "create", "update"],
                "description": "What to do",
            },
            "assistant_id": {
                "type": "string",
                "description": "Assistant ID (required for get and update)",
            },
            "name": {
                "type": "string",
                "description": "Assistant name (for create, or as a list filter)",
            },
            "instructions": {
                "type": "string",
                "description": "System instructions for the assistant (for create)",
            },
            "greeting": {
                "type": "string",
                "description": "Line spoken when a call connects (for create)",
            },
            "voice": {
                "type": "string",
                "description": "TTS voice ID (for create)",
            },
            "model": {
                "type": "string",
                "description": "LLM to back the assistant (for create)",
            },
            "updates": {
                "type": "string",
                "description": 'JSON object of fields to change, e.g. {"greeting": "Hi there"} (for update)',
            },
        },
        "required": ["action"],
    },
}

INSIGHTS_SCHEMA = {
    "name": "clawtalk_insights",
    "description": (
        "Fetch the AI-generated analysis of a finished call - summary, "
        "sentiment, and extracted topics - for a Telnyx conversation ID. The "
        "conversation ID comes from a completed call event or "
        "clawtalk_mission_event_status."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "conversation_id": {
                "type": "string",
                "description": "Telnyx conversation ID from a completed call",
            }
        },
        "required": ["conversation_id"],
    },
}


@tool("clawtalk_assistants")
def clawtalk_assistants(args: dict[str, Any], **_kwargs: Any) -> str:
    action = args.get("action")
    client = runtime().client

    if action == "list":
        assistants = client.assistants.list(name=args.get("name"))
        return ok({"assistants": assistants}, f"{len(assistants)} assistant(s)")

    if action == "get":
        (assistant_id,) = require(args, "assistant_id")
        assistant = client.assistants.get(assistant_id)
        return ok({"assistant": assistant}, f"Assistant: {assistant.get('name')}")

    if action == "create":
        name, instructions = require(args, "name", "instructions")
        assistant = client.assistants.create(
            name=name,
            instructions=instructions,
            greeting=args.get("greeting"),
            voice=args.get("voice"),
            model=args.get("model"),
        )
        return ok(
            {"assistant": assistant}, f"Created assistant: {assistant.get('id')}"
        )

    if action == "update":
        (assistant_id,) = require(args, "assistant_id")
        updates = optional_json(args.get("updates"), {})
        if not isinstance(updates, dict) or not updates:
            raise ToolError(
                "clawtalk_assistants",
                'The "updates" parameter must be a JSON object of fields to change.',
            )
        assistant = client.assistants.update(assistant_id, updates)
        return ok(
            {"assistant": assistant}, f"Updated assistant: {assistant.get('id')}"
        )

    raise ToolError("clawtalk_assistants", f"Unknown action: {action!r}")


@tool("clawtalk_insights")
def clawtalk_insights(args: dict[str, Any], **_kwargs: Any) -> str:
    (conversation_id,) = require(args, "conversation_id")
    result = runtime().client.insights.get(conversation_id)
    return ok(
        {"insights": result},
        f"Insights retrieved for conversation {conversation_id}",
    )
