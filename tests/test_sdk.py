"""REST SDK: endpoint map, transport behaviour, and envelope unwrapping."""

from __future__ import annotations

import urllib.error

import pytest

from clawtalk.sdk import ApiError, ClawTalkClient
from clawtalk.sdk.endpoints import (
    ENDPOINTS,
    IMPLEMENTED_ENDPOINTS,
    READ_ENDPOINTS,
    UNIMPLEMENTED_ENDPOINTS,
    resolve,
)


class TestEndpoints:
    def test_every_path_is_versioned(self):
        assert all(ep.path.startswith("/v1/") for ep in ENDPOINTS.values())

    def test_partitions_cover_the_map(self):
        assert len(IMPLEMENTED_ENDPOINTS) + len(UNIMPLEMENTED_ENDPOINTS) == len(ENDPOINTS)

    def test_read_endpoints_are_never_writes(self):
        assert all(not ep.write for ep in READ_ENDPOINTS.values())

    def test_resolve_interpolates_and_escapes(self):
        assert resolve("/v1/calls/:callId", {"callId": "call_123"}) == "/v1/calls/call_123"
        assert resolve("/v1/x/:id", {"id": "a/b"}) == "/v1/x/a%2Fb"

    def test_resolve_rejects_missing_param(self):
        with pytest.raises(KeyError):
            resolve("/v1/calls/:callId", {})

    def test_resolve_leaves_paramless_paths_alone(self):
        assert resolve("/v1/me") == "/v1/me"


class TestTransport:
    def test_sends_auth_and_version_headers(self, client, opener):
        opener.route("GET /v1/me", {"email": "user@example.com"})
        client.user.me()

        method, path, _body = opener.calls[0]
        assert (method, path) == ("GET", "/v1/me")

    def test_empty_body_decodes_to_empty_dict(self, client, opener):
        opener.route("GET /v1/me", "")
        assert client.user.me() == {}

    def test_http_error_becomes_api_error_with_server_code(self, client, opener):
        opener.route(
            "GET /v1/me",
            urllib.error.HTTPError(
                url="https://clawdtalk.com/v1/me",
                code=401,
                msg="Unauthorized",
                hdrs=None,
                fp=None,
            ),
        )
        with pytest.raises(ApiError) as excinfo:
            client.user.me()
        assert excinfo.value.status_code == 401

    def test_invalid_json_becomes_api_error(self, client, opener):
        opener.route("GET /v1/me", "not json at all {{{")
        with pytest.raises(ApiError):
            client.user.me()

    def test_network_error_becomes_api_error(self, client, opener):
        opener.route("GET /v1/me", urllib.error.URLError("connection refused"))
        with pytest.raises(ApiError) as excinfo:
            client.user.me()
        assert excinfo.value.status_code == 0

    def test_base_url_trailing_slash_is_normalized(self, opener):
        client = ClawTalkClient(
            api_key="k", server="https://clawdtalk.com/", opener=opener
        )
        assert client.base_url == "https://clawdtalk.com"


class TestApiErrorParsing:
    def test_extracts_code_and_message_from_error_envelope(self):
        err = ApiError(
            404, "GET /v1/x failed: 404",
            '{"error": {"code": "step_not_found", "message": "no such step"}}',
        )
        assert err.server_code == "step_not_found"
        assert err.server_message == "no such step"
        assert "no such step" in str(err)

    def test_accepts_a_flat_error_body(self):
        err = ApiError(400, "bad", '{"code": "missing_field", "detail": "to required"}')
        assert err.server_code == "missing_field"
        assert err.server_message == "to required"

    def test_short_non_json_body_is_kept(self):
        err = ApiError(502, "bad gateway", "upstream timed out")
        assert err.server_message == "upstream timed out"

    def test_long_non_json_body_is_dropped(self):
        err = ApiError(502, "bad gateway", "x" * 500)
        assert err.server_message is None


class TestNamespaceUnwrapping:
    def test_assistant_envelope_is_unwrapped(self, client, opener):
        opener.route("GET /v1/assistants/a1", {"assistant": {"id": "a1", "name": "Bot"}})
        assert client.assistants.get("a1")["name"] == "Bot"

    def test_bare_assistant_record_also_works(self, client, opener):
        opener.route("GET /v1/assistants/a1", {"id": "a1", "name": "Bot"})
        assert client.assistants.get("a1")["name"] == "Bot"

    def test_plan_data_envelope_is_unwrapped(self, client, opener):
        opener.route(
            "GET /v1/missions/m1/runs/r1/plan",
            {"data": [{"step_id": "s1", "status": "pending"}]},
        )
        steps = client.missions.plans.get("m1", "r1")
        assert [s["step_id"] for s in steps] == ["s1"]

    def test_run_create_wraps_input(self, client, opener):
        opener.route("POST /v1/missions/m1/runs", {"data": {"run_id": "r1"}})
        run = client.missions.runs.create("m1", {"original_request": "call Alice"})

        _method, _path, body = opener.calls[0]
        # The server expects the caller's object nested under "input".
        assert body == {"input": {"original_request": "call Alice"}}
        assert run["run_id"] == "r1"

    def test_schedule_selects_sms_channel_from_text_body(self, client, opener):
        opener.route("POST /v1/assistants/a1/events", {"event": {"id": "e1"}})
        client.assistants.events.schedule(
            assistant_id="a1",
            to="+15551234567",
            from_="+15557654321",
            scheduled_at="2026-09-01T15:00:00Z",
            text_body="Hi there",
        )
        _method, _path, body = opener.calls[0]
        assert body["channel"] == "sms"
        assert body["text_body"] == "Hi there"

    def test_schedule_defaults_to_call_channel(self, client, opener):
        opener.route("POST /v1/assistants/a1/events", {"event": {"id": "e1"}})
        client.assistants.events.schedule(
            assistant_id="a1",
            to="+15551234567",
            from_="+15557654321",
            scheduled_at="2026-09-01T15:00:00Z",
        )
        _method, _path, body = opener.calls[0]
        assert body["channel"] == "call"
        assert "text_body" not in body

    def test_sms_list_builds_query_string(self, client, opener):
        opener.route("GET /v1/messages", {"messages": []})
        client.sms.list(limit=5, contact="+15551234567", direction="inbound")

        _method, path, _body = opener.calls[0]
        assert "limit=5" in path and "direction=inbound" in path

    def test_missions_list_returns_empty_on_missing_key(self, client, opener):
        opener.route("GET /v1/missions", {})
        assert client.missions.list() == []
