"""The eleven mission tools - thin wrappers over :class:`MissionService`.

A mission is the unit of multi-step outreach: initialise it, give it a voice
assistant and a number, schedule calls and texts against plan steps, record
what you learn in mission memory, advance each step, and complete it.

Several tools append a step-state reminder to their result. The plan state
machine is the one part of the mission flow a model reliably forgets, and a
mission cannot be completed while any step is still open.
"""

from __future__ import annotations

import logging
from typing import Any

from ..errors import ToolError
from ..services.missions import STEP_STATUSES
from .common import ok, optional_json, require, runtime, tool

__all__ = [
    "MISSION_CANCEL_EVENT_SCHEMA",
    "MISSION_COMPLETE_SCHEMA",
    "MISSION_EVENT_STATUS_SCHEMA",
    "MISSION_GET_PLAN_SCHEMA",
    "MISSION_INIT_SCHEMA",
    "MISSION_LIST_SCHEMA",
    "MISSION_LOG_EVENT_SCHEMA",
    "MISSION_MEMORY_SCHEMA",
    "MISSION_SCHEDULE_SCHEMA",
    "MISSION_SETUP_AGENT_SCHEMA",
    "MISSION_UPDATE_STEP_SCHEMA",
]

logger = logging.getLogger(__name__)

STEP_REMINDER = (
    "Reminder: update step status as you progress "
    "(pending -> in_progress -> completed/failed/skipped). All steps must be "
    "terminal before completing the mission."
)

MISSION_PAGE_SIZE = 50


def _ok_with_reminder(payload: dict[str, Any], message: str) -> str:
    return ok({**payload, "reminder": STEP_REMINDER}, message)


# -- schemas ---------------------------------------------------------------

MISSION_INIT_SCHEMA = {
    "name": "clawtalk_mission_init",
    "description": (
        "Start a multi-step outreach mission: creates the mission, its run, "
        "and an optional plan of steps. Call this first for any campaign that "
        "needs several calls or texts over time. Idempotent - re-running with "
        "the same name resumes the existing mission instead of duplicating "
        "it. Returns the slug every other mission tool takes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Human-readable mission name; the slug is derived from it",
            },
            "instructions": {
                "type": "string",
                "description": "What the mission should achieve, for the AI agent running it",
            },
            "request": {
                "type": "string",
                "description": "The user's original request that triggered this mission",
            },
            "steps": {
                "type": "string",
                "description": (
                    "JSON array of plan steps, e.g. "
                    '[{"title": "Call Alice", "description": "Ask about the quote"}]'
                ),
            },
        },
        "required": ["name", "instructions", "request"],
    },
}

MISSION_SETUP_AGENT_SCHEMA = {
    "name": "clawtalk_mission_setup_agent",
    "description": (
        "Give a mission its voice: creates a Telnyx assistant, links it to the "
        "run, and claims a phone number to call and text from. Run this after "
        "clawtalk_mission_init and before scheduling anything. Idempotent."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "Mission slug from clawtalk_mission_init"},
            "name": {"type": "string", "description": "Assistant name"},
            "instructions": {
                "type": "string",
                "description": "What the assistant should say and do on calls",
            },
            "greeting": {
                "type": "string",
                "description": "Line spoken when the call connects",
            },
            "voice": {
                "type": "string",
                "description": "TTS voice ID; defaults to the user's configured voice",
            },
            "model": {
                "type": "string",
                "description": "LLM backing the assistant (default openai/gpt-4o)",
            },
        },
        "required": ["slug", "name", "instructions"],
    },
}

MISSION_SCHEDULE_SCHEMA = {
    "name": "clawtalk_mission_schedule",
    "description": (
        "Schedule a call or a text for a mission, using the assistant and "
        "number set up by clawtalk_mission_setup_agent. Link it to a plan step "
        "with step_id so the step advances automatically when the event "
        "completes. Returns an event_id for clawtalk_mission_event_status."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "Mission slug"},
            "channel": {
                "type": "string",
                "enum": ["call", "sms"],
                "description": "Whether to place a call or send a text",
            },
            "to": {"type": "string", "description": "Target phone number in E.164 format"},
            "scheduled_at": {
                "type": "string",
                "description": "ISO 8601 datetime to fire the event, e.g. 2026-09-01T15:00:00Z",
            },
            "text_body": {
                "type": "string",
                "description": 'Message body (required when channel is "sms")',
            },
            "step_id": {
                "type": "string",
                "description": "Plan step this event belongs to",
            },
        },
        "required": ["slug", "channel", "to", "scheduled_at"],
    },
}

MISSION_EVENT_STATUS_SCHEMA = {
    "name": "clawtalk_mission_event_status",
    "description": (
        "Check a scheduled mission call or text: whether it has fired, how the "
        "call went, and the conversation ID to pass to clawtalk_insights."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "Mission slug"},
            "event_id": {
                "type": "string",
                "description": "Scheduled event ID from clawtalk_mission_schedule",
            },
        },
        "required": ["slug", "event_id"],
    },
}

MISSION_COMPLETE_SCHEMA = {
    "name": "clawtalk_mission_complete",
    "description": (
        "Close out a mission: mark the run succeeded, record the result, and "
        "clear local state. Rejected while any plan step is still pending or "
        "in_progress - mark those completed, failed, or skipped first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "Mission slug"},
            "summary": {
                "type": "string",
                "description": "What the mission achieved, in plain language",
            },
            "payload": {
                "type": "string",
                "description": "JSON object of structured results to store with the run",
            },
        },
        "required": ["slug", "summary"],
    },
}

MISSION_UPDATE_STEP_SCHEMA = {
    "name": "clawtalk_mission_update_step",
    "description": (
        "Move a plan step through its state machine: pending -> in_progress -> "
        "completed / failed / skipped. Terminal states cannot be changed and "
        "steps cannot move backwards. Use clawtalk_mission_get_plan to see "
        "valid step IDs and their current state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "Mission slug"},
            "step_id": {"type": "string", "description": "Plan step ID"},
            "status": {
                "type": "string",
                "enum": list(STEP_STATUSES),
                "description": "New step status",
            },
        },
        "required": ["slug", "step_id", "status"],
    },
}

MISSION_LOG_EVENT_SCHEMA = {
    "name": "clawtalk_mission_log_event",
    "description": (
        "Append a note or outcome to a mission's event log, so the record "
        "shows what happened and why. Use for observations that are not step "
        "transitions, e.g. a note about what a contact said."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "Mission slug"},
            "type": {
                "type": "string",
                "description": "Event type, e.g. note, step_completed, error",
            },
            "summary": {"type": "string", "description": "One-line description of the event"},
            "step_id": {"type": "string", "description": "Related plan step ID"},
            "payload": {
                "type": "string",
                "description": "JSON object of structured event data",
            },
        },
        "required": ["slug", "type", "summary"],
    },
}

MISSION_MEMORY_SCHEMA = {
    "name": "clawtalk_mission_memory",
    "description": (
        "Persistent scratchpad for one mission. Save what you learn between "
        "calls (quotes, availability, names) so later steps and the final "
        'summary can use it. "save" sets a key, "append" adds to a list, '
        '"get" reads one key back.'
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["save", "append", "get"],
                "description": "What to do with the key",
            },
            "slug": {"type": "string", "description": "Mission slug"},
            "key": {"type": "string", "description": "Memory key"},
            "value": {
                "type": "string",
                "description": "Value to store; JSON is parsed, anything else is stored as text",
            },
        },
        "required": ["action", "slug", "key"],
    },
}

MISSION_LIST_SCHEMA = {
    "name": "clawtalk_mission_list",
    "description": (
        "List missions. By default shows locally tracked active missions with "
        'their slugs. Set "server": true to query the ClawTalk API instead, '
        "which includes finished, cancelled, and historical missions and "
        "supports status and name filters."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "server": {
                "type": "boolean",
                "description": "Query the server instead of local state",
            },
            "status": {
                "type": "string",
                "description": 'Filter by status when querying the server, e.g. "running"',
            },
            "search": {
                "type": "string",
                "description": "Filter by mission name substring when querying the server",
            },
        },
        "required": [],
    },
}

MISSION_GET_PLAN_SCHEMA = {
    "name": "clawtalk_mission_get_plan",
    "description": (
        "Show a mission's plan steps with their IDs, order, and current "
        "status. Call this before clawtalk_mission_update_step when you are "
        "unsure of a step ID, or to check what remains before completing."
    ),
    "parameters": {
        "type": "object",
        "properties": {"slug": {"type": "string", "description": "Mission slug"}},
        "required": ["slug"],
    },
}

MISSION_CANCEL_EVENT_SCHEMA = {
    "name": "clawtalk_mission_cancel_event",
    "description": (
        "Cancel a scheduled mission call or text before it fires - for example "
        "when the contact replies early and the follow-up is no longer needed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "Mission slug"},
            "event_id": {"type": "string", "description": "Scheduled event ID to cancel"},
        },
        "required": ["slug", "event_id"],
    },
}


# -- handlers --------------------------------------------------------------


@tool("clawtalk_mission_init")
def clawtalk_mission_init(args: dict[str, Any], **_kwargs: Any) -> str:
    name, instructions, request = require(args, "name", "instructions", "request")
    steps = optional_json(args.get("steps"), None)
    if steps is not None and not isinstance(steps, list):
        raise ToolError(
            "clawtalk_mission_init",
            'The "steps" parameter must be a JSON array of {title, description} objects.',
        )

    result = runtime().missions.init_mission(
        name=name, instructions=instructions, request=request, steps=steps
    )
    slug = result["slug"]
    message = (
        f"Resumed existing mission '{slug}'"
        if result["resumed"]
        else f"Created mission '{slug}' with run {result['run_id']}"
    )
    return _ok_with_reminder(result, message)


@tool("clawtalk_mission_setup_agent")
def clawtalk_mission_setup_agent(args: dict[str, Any], **_kwargs: Any) -> str:
    slug, name, instructions = require(args, "slug", "name", "instructions")
    config = runtime().config.missions

    result = runtime().missions.setup_voice_agent(
        slug=slug,
        name=name,
        instructions=instructions,
        greeting=args.get("greeting"),
        # Fall back to the operator's configured mission defaults so every
        # mission assistant sounds the same without repeating it per call.
        voice=args.get("voice") or config.default_voice,
        model=args.get("model") or config.default_model,
    )

    phone = result.get("phone")
    message = (
        f"Agent {result['assistant_id']} ready with phone {phone}"
        if phone
        else f"Agent {result['assistant_id']} created but no phone number is available"
    )
    return ok(result, message)


@tool("clawtalk_mission_schedule")
def clawtalk_mission_schedule(args: dict[str, Any], **_kwargs: Any) -> str:
    slug, channel, to, scheduled_at = require(args, "slug", "channel", "to", "scheduled_at")
    missions = runtime().missions

    if channel == "sms":
        text_body = args.get("text_body")
        if not text_body:
            raise ToolError(
                "clawtalk_mission_schedule",
                'The "text_body" parameter is required when channel is "sms".',
            )
        event_id = missions.schedule_sms(
            slug, to=to, scheduled_at=scheduled_at,
            text_body=text_body, step_id=args.get("step_id"),
        )
    elif channel == "call":
        event_id = missions.schedule_call(
            slug, to=to, scheduled_at=scheduled_at, step_id=args.get("step_id")
        )
    else:
        raise ToolError(
            "clawtalk_mission_schedule",
            f'Unknown channel {channel!r}. Use "call" or "sms".',
        )

    return _ok_with_reminder(
        {"event_id": event_id, "channel": channel, "scheduled_at": scheduled_at},
        f"Scheduled {channel} event {event_id} for {scheduled_at}",
    )


@tool("clawtalk_mission_event_status")
def clawtalk_mission_event_status(args: dict[str, Any], **_kwargs: Any) -> str:
    slug, event_id = require(args, "slug", "event_id")
    event = runtime().missions.get_scheduled_event(slug, event_id)

    status = event.get("status")
    call_status = event.get("call_status")
    suffix = f" (call: {call_status})" if call_status else ""
    return ok(
        {
            "event_id": event.get("id"),
            "channel": event.get("channel"),
            "status": status,
            "call_status": call_status,
            "conversation_id": event.get("conversation_id"),
        },
        f"Event {event.get('id')}: {status}{suffix}",
    )


@tool("clawtalk_mission_complete")
def clawtalk_mission_complete(args: dict[str, Any], **_kwargs: Any) -> str:
    slug, summary = require(args, "slug", "summary")
    payload = optional_json(args.get("payload"), None)
    if payload is not None and not isinstance(payload, dict):
        payload = {"result": payload}

    runtime().missions.complete_mission(slug, summary=summary, payload=payload)
    return ok({"slug": slug}, f"Mission '{slug}' completed successfully")


@tool("clawtalk_mission_update_step")
def clawtalk_mission_update_step(args: dict[str, Any], **_kwargs: Any) -> str:
    slug, step_id, status = require(args, "slug", "step_id", "status")
    if status not in STEP_STATUSES:
        raise ToolError(
            "clawtalk_mission_update_step",
            f"Unknown status {status!r}. Use one of: {', '.join(STEP_STATUSES)}.",
        )

    runtime().missions.update_plan_step(slug, step_id, status)
    return ok(
        {"slug": slug, "step_id": step_id, "status": status},
        f"Step '{step_id}' updated to '{status}'",
    )


@tool("clawtalk_mission_log_event")
def clawtalk_mission_log_event(args: dict[str, Any], **_kwargs: Any) -> str:
    slug, event_type, summary = require(args, "slug", "type", "summary")
    payload = optional_json(args.get("payload"), None)
    if payload is not None and not isinstance(payload, dict):
        payload = {"detail": payload}

    event_id = runtime().missions.log_event(
        slug,
        event_type=event_type,
        summary=summary,
        step_id=args.get("step_id"),
        payload=payload,
    )
    return _ok_with_reminder({"event_id": event_id}, f"Logged event: {summary}")


@tool("clawtalk_mission_memory")
def clawtalk_mission_memory(args: dict[str, Any], **_kwargs: Any) -> str:
    action, slug, key = require(args, "action", "slug", "key")
    missions = runtime().missions

    if action == "get":
        data = missions.get_memory(slug, key)
        return ok(
            {"key": key, "data": data},
            f"Memory '{key}' retrieved" if data is not None else f"No memory '{key}' found",
        )

    raw = args.get("value")
    if raw is None:
        raise ToolError(
            "clawtalk_mission_memory",
            f'The "value" parameter is required for action "{action}".',
        )
    value = optional_json(raw, raw)

    if action == "append":
        count = missions.append_memory(slug, key, value)
        return ok({"key": key, "count": count}, f"Appended to '{key}' (now {count} items)")

    if action == "save":
        missions.save_memory(slug, key, value)
        return ok({"key": key}, f"Saved memory '{key}'")

    raise ToolError("clawtalk_mission_memory", f"Unknown action: {action!r}")


@tool("clawtalk_mission_list")
def clawtalk_mission_list(args: dict[str, Any], **_kwargs: Any) -> str:
    missions = runtime().missions

    if args.get("server") is True:
        records = missions.client.missions.list(MISSION_PAGE_SIZE)

        status_filter = args.get("status")
        if status_filter:
            needle = str(status_filter).lower()
            records = [m for m in records if str(m.get("status", "")).lower() == needle]

        search = args.get("search")
        if search:
            needle = str(search).lower()
            records = [m for m in records if needle in str(m.get("name", "")).lower()]

        return ok(
            {
                "source": "server",
                "missions": [
                    {
                        "id": m.get("id"),
                        "name": m.get("name"),
                        "status": m.get("status"),
                        "channel": m.get("channel"),
                        "target_count": m.get("target_count"),
                        "events_used": m.get("events_used"),
                        "result_summary": m.get("result_summary"),
                        "created_at": m.get("created_at"),
                        "updated_at": m.get("updated_at"),
                    }
                    for m in records
                ],
            },
            f"{len(records)} mission(s) from server" if records else "No missions found",
        )

    local = missions.list_missions()
    return ok(
        {
            "source": "local",
            "missions": [
                {
                    "slug": slug,
                    "mission_name": state.get("mission_name"),
                    "mission_id": state.get("mission_id"),
                    "assistant_id": state.get("assistant_id"),
                    "agent_phone": state.get("agent_phone"),
                }
                for slug, state in local
            ],
        },
        f"{len(local)} active mission(s)" if local else "No active missions",
    )


@tool("clawtalk_mission_get_plan")
def clawtalk_mission_get_plan(args: dict[str, Any], **_kwargs: Any) -> str:
    (slug,) = require(args, "slug")
    steps = runtime().missions.get_plan(slug)
    return ok({"slug": slug, "steps": steps}, f"{len(steps)} plan step(s)")


@tool("clawtalk_mission_cancel_event")
def clawtalk_mission_cancel_event(args: dict[str, Any], **_kwargs: Any) -> str:
    slug, event_id = require(args, "slug", "event_id")
    runtime().missions.cancel_scheduled_event(slug, event_id)
    return ok({"slug": slug, "event_id": event_id}, f"Cancelled event {event_id}")
