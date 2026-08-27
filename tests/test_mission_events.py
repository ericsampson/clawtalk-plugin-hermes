"""Mission event formatting and the memory context appended to each turn."""

from __future__ import annotations

import pytest

from clawtalk import wire
from clawtalk.services.mission_events import MissionEventHandler
from clawtalk.services.missions import MissionService


@pytest.fixture
def missions(client, data_dir):
    service = MissionService(client, data_dir)
    service.update_slug_state("call-alice", {"mission_id": "m1", "run_id": "r1"})
    return service


@pytest.fixture
def handler(missions):
    return MissionEventHandler(missions)


def _event(name, **fields):
    return {"type": "event", "event": name, "mission_id": "m1", **fields}


class TestFormatting:
    def test_call_started_asks_for_a_step_update(self, handler):
        text = handler.format_event(
            _event(
                wire.EVENT_MISSION_CALL_STARTED,
                step_id="s1",
                conversation_id=None,
                **{"from": "+15557654321", "to": "+15551234567"},
            )
        )
        assert "Call started" in text
        assert "Step: s1" in text
        assert "pending" in text or "in_progress" in text

    def test_call_completed_renders_the_transcript(self, handler):
        text = handler.format_event(
            _event(
                wire.EVENT_MISSION_CALL_COMPLETED,
                step_id="s1",
                conversation_id="c1",
                duration_sec=42,
                reason="user_hangup",
                transcript=[
                    {"role": "assistant", "content": "Hi, calling about the quote"},
                    {"role": "user", "content": "It's 250"},
                ],
                **{"from": "+1", "to": "+2"},
            )
        )
        assert "[assistant]: Hi, calling about the quote" in text
        assert "[user]: It's 250" in text
        assert "last 2 messages" in text

    def test_call_completed_without_a_transcript(self, handler):
        text = handler.format_event(
            _event(
                wire.EVENT_MISSION_CALL_COMPLETED,
                transcript=[],
                **{"from": "+1", "to": "+2"},
            )
        )
        assert "(no transcript available)" in text

    def test_sms_received_includes_the_thread(self, handler):
        text = handler.format_event(
            _event(
                wire.EVENT_MISSION_SMS_RECEIVED,
                text="Yes, that works",
                thread_context=[
                    {
                        "direction": "outbound",
                        "from": "+1",
                        "to": "+2",
                        "text": "Does Tuesday suit?",
                    }
                ],
                **{"from": "+2", "to": "+1"},
            )
        )
        assert "Yes, that works" in text
        assert "Does Tuesday suit?" in text

    def test_sms_delivered_surfaces_errors(self, handler):
        text = handler.format_event(
            _event(
                wire.EVENT_MISSION_SMS_DELIVERED,
                status="failed",
                errors=["30003 unreachable"],
                **{"from": "+1", "to": "+2"},
            )
        )
        assert "30003 unreachable" in text

    def test_unknown_event_formats_to_nothing(self, handler):
        assert handler.format_event(_event("mission.something_new")) is None


class TestPrepare:
    def test_routes_to_the_slug_session(self, handler):
        prepared = handler.prepare(
            _event(
                wire.EVENT_MISSION_CALL_STARTED,
                step_id="s1",
                **{"from": "+1", "to": "+2"},
            )
        )
        assert prepared is not None
        assert prepared.slug == "call-alice"
        assert "call-alice" in prepared.channel_prompt

    def test_drops_events_for_untracked_missions(self, handler):
        event = _event(wire.EVENT_MISSION_CALL_STARTED, **{"from": "+1", "to": "+2"})
        event["mission_id"] = "unknown"
        assert handler.prepare(event) is None

    def test_appends_mission_memory(self, handler, missions):
        missions.save_memory("call-alice", "quote", {"price": 250})
        prepared = handler.prepare(
            _event(wire.EVENT_MISSION_CALL_STARTED, **{"from": "+1", "to": "+2"})
        )
        assert "Mission Memory:" in prepared.text
        assert "quote" in prepared.text

    def test_stores_the_transcript_before_the_turn(self, handler, missions):
        handler.prepare(
            _event(
                wire.EVENT_MISSION_CALL_COMPLETED,
                step_id="s1",
                conversation_id="c1",
                duration_sec=42,
                transcript=[{"role": "user", "content": "It's 250"}],
                **{"from": "+1", "to": "+2"},
            )
        )
        stored = missions.get_memory("call-alice", "transcript_s1")
        assert stored["conversation_id"] == "c1"
        assert stored["messages"][0]["content"] == "It's 250"

    def test_memory_context_is_bounded(self, handler, missions):
        for i in range(200):
            missions.save_memory("call-alice", f"key_{i}", "x" * 200)
        prepared = handler.prepare(
            _event(wire.EVENT_MISSION_CALL_STARTED, **{"from": "+1", "to": "+2"})
        )
        assert "truncated" in prepared.text
        # Bounded, not unbounded: the whole memory would be ~40k characters.
        assert len(prepared.text) < 10_000
