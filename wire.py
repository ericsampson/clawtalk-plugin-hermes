"""ClawTalk WebSocket wire protocol.

The socket is opened **outbound** by this plugin to the ClawTalk server, so
it works behind NAT, Docker, and corporate firewalls with no port forwarding.

Frame shapes::

    client -> server   {"type": "auth" | "response" | "context_response"
                        | "deep_tool_progress" | "deep_tool_result"
                        | "walkie_response" | "client_restart"
                        | "logs_response", ...}

    server -> client   {"type": "auth_ok"}
                       {"type": "auth_error", "message": ...}
                       {"type": "request_logs", "request_id": ...}
                       {"type": "event", "event": "<name>", ...payload}

Every server-pushed event is wrapped in the ``{"type": "event"}`` envelope;
:data:`EVENTS` lists the names we handle.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

__all__ = [
    "APPROVAL_DECISIONS",
    "DUPLICATE_CLIENT_CODE",
    "EVENTS",
    "MISSION_EVENTS",
    "auth",
    "client_restart",
    "context_response",
    "deep_tool_progress",
    "deep_tool_result",
    "logs_response",
    "response",
    "walkie_response",
]

#: Close code the server uses when a second client authenticates for the same
#: account. Reconnecting would just get us kicked again, so we stay down.
DUPLICATE_CLIENT_CODE = 4000

# -- inbound event names ---------------------------------------------------

EVENT_CONTEXT_REQUEST = "context_request"
EVENT_CALL_STARTED = "call.started"
EVENT_CALL_ENDED = "call.ended"
EVENT_DEEP_TOOL_REQUEST = "deep_tool_request"
EVENT_SMS_RECEIVED = "sms.received"
EVENT_APPROVAL_RESPONDED = "approval.responded"
EVENT_WALKIE_REQUEST = "walkie_request"

EVENT_MISSION_CALL_STARTED = "mission.call_started"
EVENT_MISSION_CALL_COMPLETED = "mission.call_completed"
EVENT_MISSION_CALL_FAILED = "mission.call_failed"
EVENT_MISSION_INSIGHTS_READY = "mission.insights_ready"
EVENT_MISSION_SMS_DELIVERED = "mission.sms_delivered"
EVENT_MISSION_SMS_RECEIVED = "mission.sms_received"

#: Mission lifecycle events, all routed through the mission event handler.
MISSION_EVENTS = frozenset(
    {
        EVENT_MISSION_CALL_STARTED,
        EVENT_MISSION_CALL_COMPLETED,
        EVENT_MISSION_CALL_FAILED,
        EVENT_MISSION_INSIGHTS_READY,
        EVENT_MISSION_SMS_DELIVERED,
        EVENT_MISSION_SMS_RECEIVED,
    }
)

#: Every inbound event name this plugin knows how to handle.
EVENTS = frozenset(
    {
        EVENT_CONTEXT_REQUEST,
        EVENT_CALL_STARTED,
        EVENT_CALL_ENDED,
        EVENT_DEEP_TOOL_REQUEST,
        EVENT_SMS_RECEIVED,
        EVENT_APPROVAL_RESPONDED,
        EVENT_WALKIE_REQUEST,
    }
    | MISSION_EVENTS
)

#: Terminal states an approval request can reach.
APPROVAL_DECISIONS = frozenset(
    {"approved", "denied", "timeout", "no_devices", "no_devices_reached"}
)


# -- outbound frame builders ----------------------------------------------


def auth(
    api_key: str,
    client_version: str,
    owner_name: str | None = None,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """First frame after the socket opens.

    ``owner_name``/``agent_name`` are omitted when they are still the
    defaults, so a stock install does not get auto-named server-side.
    """
    frame: dict[str, Any] = {
        "type": "auth",
        "api_key": api_key,
        "client_version": client_version,
    }
    if owner_name:
        frame["owner_name"] = owner_name
    if agent_name:
        frame["agent_name"] = agent_name
    return frame


def context_response(call_id: str, memory: str, system_prompt: str) -> dict[str, Any]:
    """Answer a ``context_request`` with the voice system prompt."""
    return {
        "type": "context_response",
        "call_id": call_id,
        "context": {"memory": memory, "system_prompt": system_prompt},
    }


def response(call_id: str, text: str) -> dict[str, Any]:
    """Speak *text* on an active call (used for the inbound greeting)."""
    return {"type": "response", "call_id": call_id, "text": text}


def deep_tool_progress(call_id: str, request_id: str, text: str) -> dict[str, Any]:
    """Interim update spoken while a deep tool request is still running."""
    return {
        "type": "deep_tool_progress",
        "call_id": call_id,
        "request_id": request_id,
        "text": text,
    }


def deep_tool_result(call_id: str, request_id: str, text: str) -> dict[str, Any]:
    """Final answer for a deep tool request."""
    return {
        "type": "deep_tool_result",
        "call_id": call_id,
        "request_id": request_id,
        "text": text,
    }


def walkie_response(
    request_id: str, reply: str = "", error: str | None = None
) -> dict[str, Any]:
    """Answer a push-to-talk transcript."""
    frame: dict[str, Any] = {
        "type": "walkie_response",
        "request_id": request_id,
        "reply": reply,
    }
    if error:
        frame["error"] = error
    return frame


def client_restart(version: str) -> dict[str, Any]:
    """Tell the server we came back after a disconnect, not a cold start."""
    return {"type": "client_restart", "version": version, "reason": "reconnect"}


def logs_response(
    request_id: str,
    lines: Sequence[str] = (),
    error: str | None = None,
) -> dict[str, Any]:
    """Answer a ``request_logs`` frame with a tail of ``ws.log``."""
    frame: dict[str, Any] = {
        "type": "logs_response",
        "request_id": request_id,
        "lines": list(lines),
    }
    if error:
        frame["error"] = error
    return frame


def transcript_lines(transcript: Sequence[dict[str, Any]] | None) -> list[str]:
    """Render a mission call transcript as indented ``[role]: content`` lines."""
    if not transcript:
        return []
    return [
        f"  [{turn.get('role', '?')}]: {turn.get('content', '')}" for turn in transcript
    ]
