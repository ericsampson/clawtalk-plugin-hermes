"""Agent tools: schema hygiene, success envelopes, and error handling."""

from __future__ import annotations

import json
import urllib.error

import pytest

from clawtalk.services.approvals import ApprovalManager
from clawtalk.services.missions import MissionService
from clawtalk.services.voice import VoiceService
from clawtalk.tools import CORE_TOOLS, MISSION_TOOLS, ToolSpec, register_tools, tool_names


@pytest.fixture
def runtime(client, data_dir, config, monkeypatch):
    """A runtime wired to the fake HTTP opener, injected into the tools."""
    from clawtalk.runtime import ClawTalkRuntime
    from clawtalk.ws_logger import WsLogger

    rt = ClawTalkRuntime(
        config=config,
        client=client,
        missions=MissionService(client, data_dir),
        approvals=ApprovalManager(client),
        voice=VoiceService(config),
        ws_log=WsLogger(data_dir / "ws.log"),
        data_dir=data_dir,
    )
    monkeypatch.setattr("clawtalk.tools.common.get_runtime", lambda: rt)
    return rt


def call(handler, **args) -> dict:
    """Invoke a tool handler and decode its JSON envelope."""
    raw = handler(args)
    assert isinstance(raw, str), "tool handlers must return a JSON string"
    return json.loads(raw)


class TestRegistry:
    def test_names_match_the_manifest(self):
        import re
        from pathlib import Path

        manifest = (Path(__file__).resolve().parent.parent / "plugin.yaml").read_text()
        declared = set(re.findall(r"^  - (clawtalk_\w+)$", manifest, re.MULTILINE))
        assert declared == set(tool_names())

    def test_every_tool_is_uniquely_named(self):
        names = tool_names()
        assert len(names) == len(set(names)) == 21

    @pytest.mark.parametrize("spec", CORE_TOOLS + MISSION_TOOLS, ids=lambda s: s.name)
    def test_schema_shape(self, spec: ToolSpec):
        schema = spec.schema
        assert schema["name"].startswith("clawtalk_")
        # A vague description is the single most common reason a model never
        # reaches for a tool.
        assert len(schema["description"]) > 60
        params = schema["parameters"]
        assert params["type"] == "object"
        assert isinstance(params["properties"], dict)
        assert isinstance(params["required"], list)
        for name in params["required"]:
            assert name in params["properties"], f"{schema['name']}: {name} not declared"
        for name, prop in params["properties"].items():
            assert prop.get("description"), f"{schema['name']}.{name} has no description"

    def test_register_tools_passes_the_toolset(self):
        seen = []

        class FakeCtx:
            def register_tool(self, **kwargs):
                seen.append(kwargs)

        count = register_tools(FakeCtx())
        assert count == 21
        assert {entry["toolset"] for entry in seen} == {"clawtalk"}
        assert all(callable(entry["handler"]) for entry in seen)

    def test_missions_can_be_left_out(self):
        seen = []

        class FakeCtx:
            def register_tool(self, **kwargs):
                seen.append(kwargs["name"])

        register_tools(FakeCtx(), include_missions=False)
        assert not any(name.startswith("clawtalk_mission_") for name in seen)


class TestErrorContract:
    def test_missing_required_arg_returns_an_error_envelope(self, runtime):
        from clawtalk.tools.call import clawtalk_call

        result = call(clawtalk_call)
        assert result["success"] is False
        assert "to" in result["error"]

    def test_server_error_is_reported_with_a_fix_hint(self, runtime, opener):
        from clawtalk.tools.mission import clawtalk_mission_update_step

        runtime.missions.update_slug_state("m", {"mission_id": "m1", "run_id": "r1"})
        opener.route(
            "GET /v1/missions/m1/runs/r1/plan",
            urllib.error.HTTPError(
                url="https://clawdtalk.com/v1/missions/m1/runs/r1/plan",
                code=404,
                msg="Not Found",
                hdrs=None,
                fp=None,
            ),
        )
        result = call(clawtalk_mission_update_step, slug="m", step_id="s1", status="in_progress")
        assert result["success"] is False

    def test_handlers_never_raise(self, runtime, opener):
        from clawtalk.tools.sms import clawtalk_sms

        opener.route("POST /v1/messages/send", urllib.error.URLError("boom"))
        result = call(clawtalk_sms, to="+15551234567", message="hi")
        assert result["success"] is False
        assert "boom" in result["error"]


class TestCallTools:
    def test_initiate(self, runtime, opener):
        from clawtalk.tools.call import clawtalk_call

        opener.route("POST /v1/calls", {"call_id": "call_1", "status": "initiating"})
        result = call(clawtalk_call, to="+15551234567", purpose="ask about the quote")

        assert result["success"] is True
        assert result["call_id"] == "call_1"
        assert "(555) 123-4567" in result["message"]
        assert opener.calls[0][2]["purpose"] == "ask about the quote"

    def test_status(self, runtime, opener):
        from clawtalk.tools.call import clawtalk_call_status

        opener.route(
            "GET /v1/calls/call_1", {"status": "answered", "duration_seconds": 12}
        )
        result = call(clawtalk_call_status, call_id="call_1")
        assert result["status"] == "answered"

    def test_end(self, runtime, opener):
        from clawtalk.tools.call import clawtalk_call_status

        opener.route("POST /v1/calls/call_1/end", {"status": "ending"})
        result = call(clawtalk_call_status, call_id="call_1", action="end")
        assert result["status"] == "ended"
        assert opener.calls[0][0] == "POST"


class TestSmsTools:
    def test_send(self, runtime, opener):
        from clawtalk.tools.sms import clawtalk_sms

        opener.route(
            "POST /v1/messages/send",
            {"id": "msg_1", "from": "+15557654321", "status": "queued"},
        )
        result = call(clawtalk_sms, to="+15551234567", message="on my way")
        assert result["message_id"] == "msg_1"
        assert opener.calls[0][2] == {"to": "+15551234567", "message": "on my way"}

    def test_send_with_media_becomes_mms(self, runtime, opener):
        from clawtalk.tools.sms import clawtalk_sms

        opener.route("POST /v1/messages/send", {"id": "msg_1"})
        call(
            clawtalk_sms,
            to="+15551234567",
            message="look",
            media_urls=["https://example.com/a.png"],
        )
        assert opener.calls[0][2]["media_urls"] == ["https://example.com/a.png"]

    def test_list_formats_numbers(self, runtime, opener):
        from clawtalk.tools.sms import clawtalk_sms_list

        opener.route(
            "GET /v1/messages",
            {
                "messages": [
                    {
                        "from": "+15551234567",
                        "to": "+15557654321",
                        "body": "hi",
                        "direction": "inbound",
                        "created_at": "2026-08-26T10:00:00Z",
                    }
                ]
            },
        )
        result = call(clawtalk_sms_list, limit=5)
        assert result["messages"][0]["from"] == "(555) 123-4567"
        assert result["total"] == 1


class TestApproveTool:
    def test_no_devices_path(self, runtime, opener):
        from clawtalk.tools.approve import clawtalk_approve

        opener.route(
            "POST /v1/approvals",
            {"request_id": "req_1", "devices_notified": 0, "devices_failed": 0},
        )
        result = call(clawtalk_approve, action="Delete the repo")
        assert result["decision"] == "no_devices"
        assert "No devices registered" in result["message"]


class TestBotConfigTool:
    def test_get(self, runtime, opener):
        from clawtalk.tools.bot_config import clawtalk_bot_config

        opener.route(
            "GET /v1/me",
            {"agent_name": "Daisy", "greeting": "Smokies Motels, how can I help?"},
        )
        result = call(clawtalk_bot_config, action="get")
        assert result["agent_name"] == "Daisy"
        assert result["bot_role"] == "personal AI assistant"

    def test_update_rejects_an_empty_change(self, runtime):
        from clawtalk.tools.bot_config import clawtalk_bot_config

        result = call(clawtalk_bot_config, action="update")
        assert result["success"] is False
        assert "No fields" in result["error"]

    def test_unknown_action(self, runtime):
        from clawtalk.tools.bot_config import clawtalk_bot_config

        result = call(clawtalk_bot_config, action="explode")
        assert result["success"] is False

    def test_list_voices_filters_and_caps(self, runtime, opener):
        from clawtalk.tools.bot_config import _voice_cache, clawtalk_bot_config

        _voice_cache.clear()
        voices = [
            {
                "id": f"Rime.v{i}",
                "name": f"voice{i}",
                "provider": "rime",
                "language": "en-US" if i % 2 else "fr-FR",
                "gender": "Female",
                "label": "x" * 200,
            }
            for i in range(60)
        ]
        opener.route(
            "GET /v1/voices",
            {"voices": voices, "providers": ["rime"], "default_voice": "Rime.v1"},
        )

        result = call(clawtalk_bot_config, action="list_voices", language="en")
        assert result["total_matching"] == 30
        assert result["showing"] == 20
        assert len(result["voices"][0]["label"]) == 80
        _voice_cache.clear()


class TestMissionTools:
    def test_init_returns_the_slug_and_a_step_reminder(self, runtime, opener):
        from clawtalk.tools.mission import clawtalk_mission_init

        opener.route("POST /v1/missions", {"mission": {"id": "m1"}})
        opener.route("POST /v1/missions/m1/runs", {"data": {"run_id": "r1"}})
        opener.route("PATCH /v1/missions/m1/runs/r1", {})

        result = call(
            clawtalk_mission_init,
            name="Call Alice",
            instructions="Get a quote",
            request="please call alice",
        )
        assert result["slug"] == "call-alice"
        assert "terminal" in result["reminder"]

    def test_init_rejects_malformed_steps(self, runtime, opener):
        from clawtalk.tools.mission import clawtalk_mission_init

        opener.route("POST /v1/missions", {"mission": {"id": "m1"}})
        result = call(
            clawtalk_mission_init,
            name="x",
            instructions="y",
            request="z",
            steps='{"not": "an array"}',
        )
        assert result["success"] is False
        assert "JSON array" in result["error"]

    def test_schedule_sms_requires_a_body(self, runtime):
        from clawtalk.tools.mission import clawtalk_mission_schedule

        result = call(
            clawtalk_mission_schedule,
            slug="m",
            channel="sms",
            to="+15551234567",
            scheduled_at="2026-09-01T15:00:00Z",
        )
        assert result["success"] is False
        assert "text_body" in result["error"]

    def test_update_step_rejects_an_unknown_status(self, runtime):
        from clawtalk.tools.mission import clawtalk_mission_update_step

        result = call(clawtalk_mission_update_step, slug="m", step_id="s1", status="donezo")
        assert result["success"] is False
        assert "Unknown status" in result["error"]

    def test_memory_save_parses_json_values(self, runtime):
        from clawtalk.tools.mission import clawtalk_mission_memory

        saved = call(
            clawtalk_mission_memory,
            action="save",
            slug="m",
            key="quote",
            value='{"price": 250}',
        )
        assert saved["success"] is True

        got = call(clawtalk_mission_memory, action="get", slug="m", key="quote")
        assert got["data"] == {"price": 250}

    def test_memory_save_keeps_plain_text_as_text(self, runtime):
        from clawtalk.tools.mission import clawtalk_mission_memory

        call(clawtalk_mission_memory, action="save", slug="m", key="note", value="ring back")
        got = call(clawtalk_mission_memory, action="get", slug="m", key="note")
        assert got["data"] == "ring back"

    def test_memory_requires_a_value_to_save(self, runtime):
        from clawtalk.tools.mission import clawtalk_mission_memory

        result = call(clawtalk_mission_memory, action="save", slug="m", key="k")
        assert result["success"] is False
        assert "value" in result["error"]

    def test_list_local(self, runtime):
        from clawtalk.tools.mission import clawtalk_mission_list

        runtime.missions.update_slug_state(
            "call-alice", {"mission_id": "m1", "mission_name": "Call Alice"}
        )
        result = call(clawtalk_mission_list)
        assert result["source"] == "local"
        assert result["missions"][0]["slug"] == "call-alice"

    def test_list_server_applies_filters(self, runtime, opener):
        from clawtalk.tools.mission import clawtalk_mission_list

        opener.route(
            "GET /v1/missions",
            {
                "missions": [
                    {"id": "m1", "name": "Call Alice", "status": "running"},
                    {"id": "m2", "name": "Call Bob", "status": "succeeded"},
                ]
            },
        )
        result = call(clawtalk_mission_list, server=True, status="running")
        assert [m["id"] for m in result["missions"]] == ["m1"]

        result = call(clawtalk_mission_list, server=True, search="bob")
        assert [m["id"] for m in result["missions"]] == ["m2"]


class TestStatusTool:
    def test_reports_disconnected_without_a_gateway(self, runtime, opener):
        from clawtalk.tools.status import clawtalk_status

        opener.route("GET /v1/me", {"email": "user@example.com", "effective_tier": "pro"})
        result = call(clawtalk_status)
        assert result["connected"] is False
        assert result["websocket_state"] == "not_running_in_this_process"
        assert result["user"] == "user@example.com"

    def test_survives_a_rejected_key(self, runtime, opener):
        from clawtalk.tools.status import clawtalk_status

        opener.route("GET /v1/me", urllib.error.URLError("unauthorized"))
        result = call(clawtalk_status)
        assert result["success"] is True
        assert "account_error" in result
