"""Approval requests: the REST create plus the WebSocket-delivered decision."""

from __future__ import annotations

import threading

import pytest

from clawtalk.services.approvals import ApprovalManager


@pytest.fixture
def approvals(client):
    return ApprovalManager(client)


def _created(request_id="req_1", notified=1, failed=0):
    return {
        "request_id": request_id,
        "status": "pending",
        "devices_notified": notified,
        "devices_failed": failed,
    }


class TestNoDevices:
    def test_no_devices_returns_immediately(self, approvals, opener):
        opener.route("POST /v1/approvals", _created(notified=0))
        assert approvals.request_approval("Delete the repo") == "no_devices"

    def test_delivery_failure_is_distinguished(self, approvals, opener):
        opener.route("POST /v1/approvals", _created(notified=0, failed=2))
        assert approvals.request_approval("Delete the repo") == "no_devices_reached"


class TestDecision:
    @pytest.mark.parametrize("decision", ["approved", "denied"])
    def test_websocket_decision_resolves_the_waiter(self, approvals, opener, decision):
        opener.route("POST /v1/approvals", _created())

        # The decision arrives on the gateway loop while the tool handler
        # blocks on a worker thread, so answer from a separate thread here.
        def answer():
            _wait_until(lambda: approvals.pending_count == 1)
            approvals.handle_ws_response({"request_id": "req_1", "decision": decision})

        thread = threading.Thread(target=answer)
        thread.start()
        try:
            assert approvals.request_approval("Send the email", timeout_s=5) == decision
        finally:
            thread.join()

    def test_timeout_when_nothing_answers(self, approvals, opener):
        opener.route("POST /v1/approvals", _created())
        assert approvals.request_approval("Send the email", timeout_s=1) == "timeout"
        assert approvals.pending_count == 0

    def test_response_for_an_unknown_request_is_ignored(self, approvals):
        approvals.handle_ws_response({"request_id": "ghost", "decision": "approved"})
        assert approvals.pending_count == 0


class TestCleanup:
    def test_disconnect_releases_every_waiter(self, approvals, opener):
        opener.route("POST /v1/approvals", _created())

        def disconnect():
            _wait_until(lambda: approvals.pending_count == 1)
            approvals.cleanup_pending()

        thread = threading.Thread(target=disconnect)
        thread.start()
        try:
            # Without cleanup this would block for the full 30s timeout.
            assert approvals.request_approval("Wire the money", timeout_s=30) == "timeout"
        finally:
            thread.join()
        assert approvals.pending_count == 0


class TestRequestBody:
    def test_biometric_and_expiry_reach_the_server(self, approvals, opener):
        opener.route("POST /v1/approvals", _created(notified=0))
        approvals.request_approval(
            "Wire $5000", details="to Acme Ltd", biometric=True, timeout_s=120
        )

        body = opener.calls[0][2]
        assert body == {
            "action": "Wire $5000",
            "require_biometric": True,
            "details": "to Acme Ltd",
            "expires_in": 120,
        }


def _wait_until(predicate, timeout=5.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    raise AssertionError("condition not met in time")
