"""SMS tools: send, list history, list conversations."""

from __future__ import annotations

import logging
from typing import Any

from ..formatting import format_phone_number
from .common import ok, require, runtime, tool

__all__ = [
    "SMS_CONVERSATIONS_SCHEMA",
    "SMS_LIST_SCHEMA",
    "SMS_SCHEMA",
    "clawtalk_sms",
    "clawtalk_sms_conversations",
    "clawtalk_sms_list",
]

logger = logging.getLogger(__name__)

SMS_SCHEMA = {
    "name": "clawtalk_sms",
    "description": (
        "Send a text message (or an MMS with media) from the user's ClawTalk "
        "number. Use when the user asks to text, SMS, or message a phone "
        "number. For a reply to an incoming text, this is also the tool - the "
        "conversation with that contact is kept per number."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": "Destination phone number in E.164 format, e.g. +353851234567",
            },
            "message": {"type": "string", "description": "Message body"},
            "media_urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Publicly reachable media URLs to attach, turning this into an MMS",
            },
        },
        "required": ["to", "message"],
    },
}

SMS_LIST_SCHEMA = {
    "name": "clawtalk_sms_list",
    "description": (
        "List recent SMS/MMS messages across all contacts, newest first. "
        "Filter by contact number or direction. Use to answer questions like "
        '"what did she text me" or "did my message send".'
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Maximum messages to return (default 20)",
            },
            "contact": {
                "type": "string",
                "description": "Only messages to or from this phone number",
            },
            "direction": {
                "type": "string",
                "enum": ["inbound", "outbound"],
                "description": "Only messages in this direction",
            },
        },
        "required": [],
    },
}

SMS_CONVERSATIONS_SCHEMA = {
    "name": "clawtalk_sms_conversations",
    "description": (
        "List every SMS conversation with its most recent message and unread "
        "count. Use to get an overview of who has been texting before drilling "
        "into one thread with clawtalk_sms_list."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


@tool("clawtalk_sms")
def clawtalk_sms(args: dict[str, Any], **_kwargs: Any) -> str:
    to, message = require(args, "to", "message")
    logger.info("[clawtalk] sending SMS to %s", to)

    result = runtime().client.sms.send(
        to=to, message=message, media_urls=args.get("media_urls")
    )

    pretty = format_phone_number(to)
    message_id = result.get("id", "")
    return ok(
        {
            "message_id": message_id,
            "from": format_phone_number(result.get("from") or ""),
            "status": result.get("status") or "sent",
        },
        f"SMS sent to {pretty}. Message ID: {message_id}",
    )


@tool("clawtalk_sms_list")
def clawtalk_sms_list(args: dict[str, Any], **_kwargs: Any) -> str:
    logger.info("[clawtalk] listing SMS messages")
    result = runtime().client.sms.list(
        limit=args.get("limit"),
        contact=args.get("contact"),
        direction=args.get("direction"),
    )

    messages = [
        {
            "from": format_phone_number(m.get("from") or ""),
            "to": format_phone_number(m.get("to") or ""),
            "body": m.get("body"),
            "direction": m.get("direction"),
            "created_at": m.get("created_at"),
        }
        for m in (result.get("messages") or [])
    ]
    return ok(
        {"messages": messages, "total": len(messages)},
        f"{len(messages)} message(s)",
    )


@tool("clawtalk_sms_conversations")
def clawtalk_sms_conversations(_args: dict[str, Any], **_kwargs: Any) -> str:
    logger.info("[clawtalk] listing SMS conversations")
    result = runtime().client.sms.conversations()

    conversations = [
        {
            "contact": format_phone_number(c.get("contact") or ""),
            "last_message": c.get("last_message"),
            "last_message_at": c.get("last_message_at"),
            "unread_count": c.get("unread_count") or 0,
        }
        for c in (result.get("conversations") or [])
    ]
    return ok(
        {"conversations": conversations},
        f"{len(conversations)} conversation(s)",
    )
