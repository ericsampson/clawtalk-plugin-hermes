"""Real-time mission event handling.

Turns a ``mission.*`` WebSocket event into the prompt and per-turn system
prompt that the gateway adapter dispatches into that mission's Hermes
session (``clawtalk`` platform, chat id ``mission:<slug>``).

Splitting *formatting* from *dispatch* keeps this module pure and easily
testable: :meth:`MissionEventHandler.prepare` performs the blocking work
(state lookup, transcript persistence, memory read) and returns a plain
value; the adapter owns the async side.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .. import wire
from .missions import MissionService

__all__ = ["MissionEventHandler", "PreparedMissionEvent"]

logger = logging.getLogger(__name__)

MAX_MEMORY_CHARS = 4000


@dataclass(frozen=True)
class PreparedMissionEvent:
    """Everything needed to run one mission-event agent turn."""

    slug: str
    mission_id: str
    #: The formatted event, plus mission memory, as the user-visible prompt.
    text: str
    #: Ephemeral system prompt for this turn only.
    channel_prompt: str


class MissionEventHandler:
    """Formats mission events and enriches them with mission memory."""

    def __init__(self, missions: MissionService) -> None:
        self._missions = missions

    def prepare(self, message: Mapping[str, Any]) -> PreparedMissionEvent | None:
        """Build the turn for one ``mission.*`` event.

        Returns ``None`` when the event is unrecognised or its mission is not
        tracked locally - there is no session to route it to in that case.

        Performs blocking I/O; call it from a worker thread.
        """
        event = str(message.get("event") or "")
        text = self.format_event(message)
        if not text:
            return None

        mission_id = str(message.get("mission_id") or "")
        slug = self._missions.resolve_slug(mission_id)
        if not slug:
            logger.warning(
                "[clawtalk] no local state for mission %s, dropping %s",
                mission_id,
                event,
            )
            return None

        # Persist the transcript before the turn so the agent can also reach
        # it later through clawtalk_mission_memory.
        if event == wire.EVENT_MISSION_CALL_COMPLETED:
            self._store_transcript(slug, message)

        memory_context = self._memory_context(slug)
        full_text = f"{text}\n\n{memory_context}" if memory_context else text

        return PreparedMissionEvent(
            slug=slug,
            mission_id=mission_id,
            text=full_text,
            channel_prompt=self.build_system_prompt(slug, mission_id),
        )

    # -- prompts -----------------------------------------------------------

    @staticmethod
    def build_system_prompt(slug: str, mission_id: str) -> str:
        """Remind the session which mission it owns and how steps move."""
        return "\n".join(
            [
                f'You are handling events for mission "{slug}" (ID: {mission_id}).',
                "You have access to ClawTalk mission tools. Use them to progress this mission.",
                "",
                "When you receive a mission event:",
                "1. Review the event details carefully",
                "2. Extract key information and save to mission memory if relevant",
                "3. Update the step status (e.g. in_progress -> completed)",
                "4. If all steps are done and the mission goal is achieved, complete the mission",
                "5. If a step failed, decide whether to retry or mark it failed",
                "",
                "Step state machine: pending -> in_progress -> completed/failed/skipped. "
                "No backwards transitions.",
                "You cannot complete a mission while steps are still pending or in_progress.",
            ]
        )

    # -- formatting --------------------------------------------------------

    def format_event(self, message: Mapping[str, Any]) -> str | None:
        """Render one mission event as an instruction-bearing prompt."""
        formatters = {
            wire.EVENT_MISSION_CALL_STARTED: self._format_call_started,
            wire.EVENT_MISSION_CALL_COMPLETED: self._format_call_completed,
            wire.EVENT_MISSION_CALL_FAILED: self._format_call_failed,
            wire.EVENT_MISSION_INSIGHTS_READY: self._format_insights_ready,
            wire.EVENT_MISSION_SMS_DELIVERED: self._format_sms_delivered,
            wire.EVENT_MISSION_SMS_RECEIVED: self._format_sms_received,
        }
        formatter = formatters.get(str(message.get("event") or ""))
        return formatter(message) if formatter else None

    @staticmethod
    def _step_suffix(message: Mapping[str, Any]) -> str:
        step_id = message.get("step_id")
        return f" | Step: {step_id}" if step_id else ""

    def _format_call_started(self, msg: Mapping[str, Any]) -> str:
        return (
            "[Mission Event] Call started\n"
            f"Mission: {msg.get('mission_id')}{self._step_suffix(msg)}\n"
            f"From: {msg.get('from')} -> To: {msg.get('to')}\n"
            f"Conversation ID: {msg.get('conversation_id') or 'pending'}\n"
            "Action: Update step to in_progress if not already done."
        )

    def _format_call_completed(self, msg: Mapping[str, Any]) -> str:
        transcript = msg.get("transcript") or []
        lines = wire.transcript_lines(transcript)
        body = "\n".join(lines) if lines else "  (no transcript available)"
        return (
            "[Mission Event] Call completed\n"
            f"Mission: {msg.get('mission_id')}{self._step_suffix(msg)}\n"
            f"From: {msg.get('from')} -> To: {msg.get('to')}\n"
            f"Duration: {msg.get('duration_sec') if msg.get('duration_sec') is not None else '?'}s"
            f" | Reason: {msg.get('reason') or 'hangup'}\n"
            f"Conversation ID: {msg.get('conversation_id') or 'unknown'}\n\n"
            f"Transcript (last {len(transcript)} messages):\n{body}\n\n"
            "Action: Review the transcript, extract key information, save to "
            "mission memory, and update the step status."
        )

    def _format_call_failed(self, msg: Mapping[str, Any]) -> str:
        return (
            "[Mission Event] Call FAILED\n"
            f"Mission: {msg.get('mission_id')}{self._step_suffix(msg)}\n"
            f"From: {msg.get('from')} -> To: {msg.get('to')}\n"
            f"Reason: {msg.get('reason')}\n\n"
            "Action: Decide if this is retryable (no-answer, busy -> reschedule) "
            "or terminal (-> mark step failed)."
        )

    def _format_insights_ready(self, msg: Mapping[str, Any]) -> str:
        return (
            "[Mission Event] AI insights ready\n"
            f"Mission: {msg.get('mission_id')}{self._step_suffix(msg)}\n"
            f"Conversation ID: {msg.get('conversation_id') or 'unknown'}\n\n"
            f"Summary: {msg.get('summary') or '(empty)'}\n\n"
            "Action: Save insights to mission memory for final analysis."
        )

    def _format_sms_delivered(self, msg: Mapping[str, Any]) -> str:
        errors = msg.get("errors") or []
        error_text = f"\nErrors: {', '.join(str(e) for e in errors)}" if errors else ""
        return (
            f"[Mission Event] SMS {msg.get('status')}\n"
            f"Mission: {msg.get('mission_id')}{self._step_suffix(msg)}\n"
            f"From: {msg.get('from')} -> To: {msg.get('to')}\n"
            f"Status: {msg.get('status')}{error_text}\n\n"
            "Action: Update step status based on delivery result."
        )

    def _format_sms_received(self, msg: Mapping[str, Any]) -> str:
        thread = msg.get("thread_context") or []
        thread_text = ""
        if thread:
            lines = [
                f"  [{turn.get('direction')}] {turn.get('from')} -> "
                f"{turn.get('to')}: {turn.get('text')}"
                for turn in thread
            ]
            thread_text = (
                f"\nConversation thread (last {len(thread)} messages):\n"
                + "\n".join(lines)
                + "\n"
            )

        return (
            "[Mission Event] SMS reply received\n"
            f"Mission: {msg.get('mission_id')}{self._step_suffix(msg)}\n"
            f"From: {msg.get('from')} -> To: {msg.get('to')}\n\n"
            f"Message: {msg.get('text')}\n"
            f"{thread_text}\n"
            "Action: Review the reply, extract key information, save to mission "
            "memory, and update the step status. If the mission goal is achieved, "
            "complete the mission."
        )

    # -- context -----------------------------------------------------------

    def _memory_context(self, slug: str) -> str | None:
        """Render mission memory as a bounded block appended to the prompt."""
        try:
            memory = self._missions.get_memory(slug)
        except Exception:  # noqa: BLE001 - context is best-effort
            return None
        if not isinstance(memory, Mapping) or not memory:
            return None

        lines: list[str] = []
        total = 0
        for key, value in memory.items():
            rendered = value if isinstance(value, str) else _compact_json(value)
            line = f"  {key}: {rendered}"
            if total + len(line) > MAX_MEMORY_CHARS:
                lines.append(f"  ... ({len(memory) - len(lines)} more keys truncated)")
                break
            lines.append(line)
            total += len(line)

        return "Mission Memory:\n" + "\n".join(lines)

    def _store_transcript(self, slug: str, msg: Mapping[str, Any]) -> None:
        step_id = msg.get("step_id")
        transcript = msg.get("transcript") or []
        if not step_id or not transcript:
            return

        try:
            self._missions.save_memory(
                slug,
                f"transcript_{step_id}",
                {
                    "conversation_id": msg.get("conversation_id"),
                    "duration_sec": msg.get("duration_sec"),
                    "reason": msg.get("reason"),
                    "messages": list(transcript),
                    "stored_at": datetime.now(UTC).isoformat(),
                },
            )
            logger.info("[clawtalk] transcript stored for %s/%s", slug, step_id)
        except Exception as exc:  # noqa: BLE001 - never block the turn
            logger.warning("[clawtalk] failed to store transcript: %s", exc)


def _compact_json(value: Any) -> str:
    import json

    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
