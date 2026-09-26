"""Config resolution, the WebSocket wire helpers, and the traffic log."""

from __future__ import annotations

import json

import pytest

from clawtalk import wire
from clawtalk.config import DEFAULT_SERVER, load_config
from clawtalk.services.voice import DEFAULT_VOICE_CONTEXT, VoiceService
from clawtalk.ws_logger import WsLogger, redact


class TestConfig:
    def test_defaults(self, monkeypatch):
        for name in list(dict(__import__("os").environ)):
            if name.startswith("CLAWTALK_"):
                monkeypatch.delenv(name, raising=False)

        config = load_config()
        assert config.server == DEFAULT_SERVER
        assert config.owner_name == "there"
        assert config.agent_name == "ClawTalk"
        assert config.greeting == "Hey there, what's up?"
        assert config.auto_connect is True
        assert config.has_api_key is False

    def test_env_beats_extra(self, monkeypatch):
        monkeypatch.setenv("CLAWTALK_API_KEY", "from_env")
        config = load_config(extra={"api_key": "from_yaml"})
        assert config.api_key == "from_env"

    def test_extra_is_used_when_env_is_unset(self, monkeypatch):
        monkeypatch.delenv("CLAWTALK_API_KEY", raising=False)
        config = load_config(extra={"api_key": "from_yaml"})
        assert config.api_key == "from_yaml"

    def test_home_channel_resolves_from_extra_and_env_wins(self, monkeypatch):
        monkeypatch.delenv("CLAWTALK_HOME_CHANNEL", raising=False)
        config = load_config(extra={"home_channel": "sms:yaml-target"})
        assert config.home_channel == "sms:yaml-target"

        config = load_config(extra={"home_channel": {"chat_id": "sms:mapped-target"}})
        assert config.home_channel == "sms:mapped-target"

        monkeypatch.setenv("CLAWTALK_HOME_CHANNEL", "sms:env-target")
        config = load_config(extra={"home_channel": "sms:yaml-target"})
        assert config.home_channel == "sms:env-target"

    def test_greeting_interpolates_owner_name(self, monkeypatch):
        monkeypatch.delenv("CLAWTALK_GREETING", raising=False)
        monkeypatch.delenv("CLAWTALK_OWNER_NAME", raising=False)
        config = load_config(
            extra={"owner_name": "Rudra", "greeting": "Hi {ownerName}, ready?"}
        )
        assert config.greeting == "Hi Rudra, ready?"

    def test_allowed_users_parses_a_csv_string(self, monkeypatch):
        monkeypatch.setenv("CLAWTALK_ALLOWED_USERS", "+155****4567, +155****4321")
        config = load_config()
        assert config.allowed_users == ("+155****4567", "+155****4321")

    def test_observer_disabled_when_missions_are_off(self, monkeypatch):
        monkeypatch.delenv("CLAWTALK_MISSIONS_ENABLED", raising=False)
        monkeypatch.delenv("CLAWTALK_MISSION_OBSERVER_ENABLED", raising=False)
        config = load_config(extra={"missions": {"enabled": False}})
        assert config.missions.enabled is False
        assert config.missions.observer.enabled is False

    @pytest.mark.parametrize(
        ("server", "expected"),
        [
            ("https://clawdtalk.com", "wss://clawdtalk.com/ws"),
            ("https://clawdtalk.com/", "wss://clawdtalk.com/ws"),
            ("http://localhost:3000", "ws://localhost:3000/ws"),
        ],
    )
    def test_ws_url(self, server, expected, monkeypatch):
        monkeypatch.delenv("CLAWTALK_SERVER", raising=False)
        assert load_config(extra={"server": server}).ws_url == expected


class TestWireFrames:
    def test_auth_omits_default_names(self):
        frame = wire.auth("key", "0.1.0")
        assert frame == {"type": "auth", "api_key": "key", "client_version": "0.1.0"}

    def test_auth_includes_custom_names(self):
        frame = wire.auth("key", "0.1.0", owner_name="Rudra", agent_name="Daisy")
        assert frame["owner_name"] == "Rudra"
        assert frame["agent_name"] == "Daisy"

    def test_deep_tool_result_carries_the_request_id(self):
        frame = wire.deep_tool_result("call_1", "req_1", "Done.")
        assert frame == {
            "type": "deep_tool_result",
            "call_id": "call_1",
            "request_id": "req_1",
            "text": "Done.",
        }

    def test_walkie_response_omits_an_absent_error(self):
        assert "error" not in wire.walkie_response("req_1", reply="Sure")

    def test_all_mission_events_are_known_events(self):
        assert wire.MISSION_EVENTS <= wire.EVENTS

    def test_transcript_lines(self):
        lines = wire.transcript_lines(
            [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}]
        )
        assert lines == ["  [user]: hi", "  [assistant]: hey"]
        assert wire.transcript_lines(None) == []


class TestVoiceService:
    def test_default_context_is_the_full_prompt(self, config):
        context = VoiceService(config).build_context()
        assert "VOICE RULES" in context
        assert "DRIP PROGRESS UPDATES" in context
        assert "APPROVAL REQUESTS" in context

    def test_no_identity_section_for_stock_config(self, config):
        assert "IDENTITY:" not in VoiceService(config).build_context()

    def test_identity_section_when_names_are_customised(self, config):
        import dataclasses

        named = dataclasses.replace(config, owner_name="Rudra", agent_name="Daisy")
        context = VoiceService(named).build_context()
        assert "Your name is Daisy." in context
        assert "speaking with Rudra" in context

    def test_custom_voice_context_replaces_the_body(self, config):
        import dataclasses

        custom = dataclasses.replace(config, voice_context="Be terse.")
        context = VoiceService(custom).build_context()
        assert context == "Be terse."
        assert DEFAULT_VOICE_CONTEXT not in context


class TestWsLogger:
    def test_redacts_secrets_at_any_depth(self):
        redacted = redact(
            {"type": "auth", "api_key": "secret", "nested": {"token": "t", "ok": 1}}
        )
        assert redacted["api_key"] == "[REDACTED]"
        assert redacted["nested"]["token"] == "[REDACTED]"
        assert redacted["nested"]["ok"] == 1

    def test_redaction_is_depth_bounded(self):
        deep: dict = {}
        cursor = deep
        for _ in range(20):
            cursor["next"] = {}
            cursor = cursor["next"]
        assert "[max depth]" in json.dumps(redact(deep))

    def test_writes_and_reads_back(self, data_dir):
        log = WsLogger(data_dir / "ws.log")
        log.open()
        log.lifecycle("connected", "wss://clawdtalk.com/ws")
        log.outbound({"type": "auth", "api_key": "secret"})
        log.inbound({"type": "auth_ok"})
        log.close()

        lines = log.read_recent_lines()
        assert len(lines) == 3
        assert "secret" not in "\n".join(lines)
        assert "[REDACTED]" in lines[1]

    def test_suppresses_log_plumbing(self, data_dir):
        log = WsLogger(data_dir / "ws.log")
        log.open()
        log.inbound({"type": "request_logs", "request_id": "r1"})
        log.outbound({"type": "logs_response", "request_id": "r1", "lines": []})
        log.close()
        # Logging the log tail would feed itself on every round trip.
        assert log.read_recent_lines() == []

    def test_survives_an_unwritable_path(self, data_dir):
        log = WsLogger(data_dir / "missing" / "nested" / "ws.log")
        log.open()
        log.lifecycle("connected")
        log.close()
