"""Mission state file, lifecycle, and the plan-step state machine."""

from __future__ import annotations

import json

import pytest

from clawtalk.services.missions import MissionService


@pytest.fixture
def missions(client, data_dir):
    return MissionService(client, data_dir)


class TestState:
    def test_missing_file_reads_as_empty(self, missions):
        assert missions.load_state() == {}

    def test_corrupt_file_reads_as_empty(self, missions):
        missions.state_path.write_text("{not json", encoding="utf-8")
        assert missions.load_state() == {}

    def test_update_merges_rather_than_replaces(self, missions):
        missions.update_slug_state("m", {"mission_id": "m1"})
        missions.update_slug_state("m", {"run_id": "r1"})
        assert missions.get_slug_state("m") == {"mission_id": "m1", "run_id": "r1"}

    def test_remove_is_idempotent(self, missions):
        missions.update_slug_state("m", {"mission_id": "m1"})
        missions.remove_slug_state("m")
        missions.remove_slug_state("m")
        assert missions.get_slug_state("m") == {}

    def test_write_is_atomic_and_leaves_no_temp_file(self, missions):
        missions.update_slug_state("m", {"mission_id": "m1"})
        siblings = list(missions.state_path.parent.iterdir())
        assert [p.name for p in siblings] == ["missions_state.json"]
        assert json.loads(missions.state_path.read_text())["m"]["mission_id"] == "m1"

    def test_resolve_slug_maps_server_id_back(self, missions):
        missions.update_slug_state("call-alice", {"mission_id": "m1"})
        assert missions.resolve_slug("m1") == "call-alice"
        assert missions.resolve_slug("nope") is None


class TestMemory:
    def test_save_and_get_roundtrip(self, missions):
        missions.save_memory("m", "quote", {"price": 250})
        assert missions.get_memory("m", "quote") == {"price": 250}

    def test_get_without_key_returns_all(self, missions):
        missions.save_memory("m", "a", 1)
        missions.save_memory("m", "b", 2)
        assert missions.get_memory("m") == {"a": 1, "b": 2}

    def test_append_creates_the_list(self, missions):
        assert missions.append_memory("m", "calls", "first") == 1
        assert missions.get_memory("m", "calls") == ["first"]

    def test_append_promotes_an_existing_scalar(self, missions):
        missions.save_memory("m", "calls", "first")
        assert missions.append_memory("m", "calls", "second") == 2
        assert missions.get_memory("m", "calls") == ["first", "second"]

    def test_save_stamps_last_updated(self, missions):
        missions.save_memory("m", "a", 1)
        assert missions.get_slug_state("m")["last_updated"].endswith("Z")


class TestInitMission:
    def test_creates_mission_run_and_plan(self, missions, opener):
        opener.route("POST /v1/missions", {"mission": {"id": "m1"}})
        opener.route("POST /v1/missions/m1/runs", {"data": {"run_id": "r1"}})
        opener.route("POST /v1/missions/m1/runs/r1/plan", {"data": []})
        opener.route("PATCH /v1/missions/m1/runs/r1", {})

        result = missions.init_mission(
            name="Call Alice",
            instructions="Get a quote",
            request="please call alice",
            steps=[{"title": "Ring her", "description": "ask about price"}],
        )

        assert result == {
            "mission_id": "m1",
            "run_id": "r1",
            "slug": "call-alice",
            "resumed": False,
        }

        plan_call = next(c for c in opener.calls if c[1].endswith("/plan"))
        step = plan_call[2]["steps"][0]
        assert step == {
            "step_id": "ring-her",
            "sequence": 1,
            "title": "Ring her",
            "description": "ask about price",
            "status": "pending",
        }

        # The run must be moved to running, or the observer will never see it.
        assert any(c[0] == "PATCH" and c[2] == {"status": "running"} for c in opener.calls)

    def test_resumes_instead_of_duplicating(self, missions, opener):
        missions.update_slug_state("call-alice", {"mission_id": "m1", "run_id": "r1"})

        result = missions.init_mission(
            name="Call Alice", instructions="x", request="y"
        )

        assert result["resumed"] is True
        assert opener.calls == []


class TestSetupVoiceAgent:
    def test_creates_links_and_claims_a_number(self, missions, opener):
        missions.update_slug_state("m", {"mission_id": "m1", "run_id": "r1"})
        opener.route("POST /v1/assistants", {"assistant": {"id": "a1"}})
        opener.route("POST /v1/missions/m1/runs/r1/agents", {})
        opener.route(
            "GET /v1/numbers/account-phones/available",
            {"phone": {"id": "p1", "phone_number": "+15557654321"}},
        )

        result = missions.setup_voice_agent(slug="m", name="Bot", instructions="be nice")

        assert result == {"assistant_id": "a1", "phone": "+15557654321"}
        assert missions.get_slug_state("m")["phone_number_id"] == "p1"

    def test_survives_having_no_number_available(self, missions, opener):
        import urllib.error

        missions.update_slug_state("m", {"mission_id": "m1", "run_id": "r1"})
        opener.route("POST /v1/assistants", {"assistant": {"id": "a1"}})
        opener.route("POST /v1/missions/m1/runs/r1/agents", {})
        opener.route(
            "GET /v1/numbers/account-phones/available",
            urllib.error.URLError("no numbers"),
        )

        result = missions.setup_voice_agent(slug="m", name="Bot", instructions="be nice")
        assert result == {"assistant_id": "a1", "phone": None}

    def test_is_idempotent(self, missions, opener):
        missions.update_slug_state(
            "m", {"assistant_id": "a1", "agent_phone": "+15557654321"}
        )
        result = missions.setup_voice_agent(slug="m", name="Bot", instructions="x")
        assert result == {"assistant_id": "a1", "phone": "+15557654321"}
        assert opener.calls == []


class TestPlanStepStateMachine:
    def _plan(self, opener, status):
        opener.route(
            "GET /v1/missions/m1/runs/r1/plan",
            {"data": [{"step_id": "s1", "status": status}]},
        )
        opener.route("PATCH /v1/missions/m1/runs/r1/plan/steps/s1", {"data": {}})

    @pytest.fixture
    def ready(self, missions):
        missions.update_slug_state("m", {"mission_id": "m1", "run_id": "r1"})
        return missions

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            ("pending", "in_progress"),
            ("pending", "skipped"),
            ("in_progress", "completed"),
            ("in_progress", "failed"),
            ("in_progress", "skipped"),
        ],
    )
    def test_allows_valid_transitions(self, ready, opener, current, target):
        self._plan(opener, current)
        ready.update_plan_step("m", "s1", target)

    @pytest.mark.parametrize("terminal", ["completed", "failed", "skipped"])
    def test_refuses_to_move_a_terminal_step(self, ready, opener, terminal):
        self._plan(opener, terminal)
        with pytest.raises(ValueError, match="terminal state"):
            ready.update_plan_step("m", "s1", "in_progress")

    def test_refuses_to_skip_straight_to_completed(self, ready, opener):
        self._plan(opener, "pending")
        with pytest.raises(ValueError, match="Invalid transition"):
            ready.update_plan_step("m", "s1", "completed")

    def test_reports_an_unknown_step(self, ready, opener):
        opener.route("GET /v1/missions/m1/runs/r1/plan", {"data": []})
        with pytest.raises(ValueError, match="not found"):
            ready.update_plan_step("m", "missing", "in_progress")

    def test_requires_an_initialised_mission(self, missions):
        with pytest.raises(ValueError, match="No active mission"):
            missions.update_plan_step("nope", "s1", "in_progress")


class TestCompleteMission:
    @pytest.fixture
    def ready(self, missions):
        missions.update_slug_state("m", {"mission_id": "m1", "run_id": "r1"})
        return missions

    def test_completes_when_every_step_is_terminal(self, ready, opener):
        opener.route(
            "GET /v1/missions/m1/runs/r1/plan",
            {"data": [{"step_id": "s1", "status": "completed"}]},
        )
        opener.route("PATCH /v1/missions/m1/runs/r1", {})

        ready.complete_mission("m", summary="Got the quote", payload={"price": 250})

        patch = next(c for c in opener.calls if c[0] == "PATCH")
        assert patch[2]["status"] == "succeeded"
        assert patch[2]["result_summary"] == "Got the quote"
        # Local state is cleared so the slug can be reused.
        assert ready.get_slug_state("m") == {}

    def test_refuses_while_a_step_is_open(self, ready, opener):
        opener.route(
            "GET /v1/missions/m1/runs/r1/plan",
            {"data": [{"step_id": "s1", "status": "in_progress"}]},
        )
        with pytest.raises(ValueError, match="non-terminal"):
            ready.complete_mission("m", summary="done")
        # State survives the refusal so the mission can still be finished.
        assert ready.get_slug_state("m")["mission_id"] == "m1"


class TestScheduling:
    def test_call_requires_an_assistant(self, missions):
        missions.update_slug_state("m", {"mission_id": "m1", "run_id": "r1"})
        with pytest.raises(ValueError, match="no assistant"):
            missions.schedule_call("m", to="+15551234567", scheduled_at="2026-09-01T15:00:00Z")

    def test_sms_passes_body_and_step(self, missions, opener):
        missions.update_slug_state(
            "m",
            {
                "mission_id": "m1",
                "run_id": "r1",
                "assistant_id": "a1",
                "agent_phone": "+15557654321",
            },
        )
        opener.route("POST /v1/assistants/a1/events", {"event": {"id": "e1"}})

        event_id = missions.schedule_sms(
            "m",
            to="+15551234567",
            scheduled_at="2026-09-01T15:00:00Z",
            text_body="Following up",
            step_id="s1",
        )

        assert event_id == "e1"
        body = opener.calls[0][2]
        assert body["channel"] == "sms"
        assert body["step_id"] == "s1"
        assert body["from"] == "+15557654321"
