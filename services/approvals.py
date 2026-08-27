"""Push-notification approval lifecycle.

Creates the approval request over REST, then blocks until the user's decision
arrives over the WebSocket. There is no polling.

Threading note: the request is created from a Hermes tool handler (which may
run on a worker thread) while the decision arrives on the gateway event loop.
The pending map is therefore guarded by a ``threading.Lock`` and each waiter
blocks on a ``threading.Event``, which is safe from either side.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..sdk import ClawTalkClient

__all__ = ["ApprovalDecision", "ApprovalManager"]

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 60

#: Every terminal state a request can settle into.
ApprovalDecision = str

APPROVED = "approved"
DENIED = "denied"
TIMEOUT = "timeout"
NO_DEVICES = "no_devices"
NO_DEVICES_REACHED = "no_devices_reached"


@dataclass
class _Pending:
    event: threading.Event
    decision: str | None = None


class ApprovalManager:
    """Create approval requests and await their WebSocket decision."""

    def __init__(self, client: ClawTalkClient) -> None:
        self._client = client
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    # -- request -----------------------------------------------------------

    def request_approval(
        self,
        action: str,
        *,
        details: str | None = None,
        biometric: bool = False,
        timeout_s: int | None = None,
    ) -> ApprovalDecision:
        """Ask the user to approve *action* and block until they answer.

        Returns one of ``approved``, ``denied``, ``timeout``, ``no_devices``,
        or ``no_devices_reached``.
        """
        timeout = int(timeout_s or DEFAULT_TIMEOUT_S)
        logger.info("[clawtalk] requesting approval: %s", action)

        result = self._client.approvals.create(
            action=action,
            details=details,
            require_biometric=biometric,
            expires_in=timeout,
        )

        request_id = str(result.get("request_id") or "")
        notified = int(result.get("devices_notified") or 0)
        failed = int(result.get("devices_failed") or 0)

        logger.info(
            "[clawtalk] approval created: %s (notified: %d, failed: %d)",
            request_id,
            notified,
            failed,
        )

        # Nothing to wait for - the notification never left the building.
        if notified == 0:
            return NO_DEVICES_REACHED if failed > 0 else NO_DEVICES

        decision = self._wait_for_decision(request_id, timeout)
        logger.info("[clawtalk] approval result: %s", decision)
        return decision

    # -- WebSocket side ----------------------------------------------------

    def handle_ws_response(self, message: Mapping[str, Any]) -> None:
        """Resolve the waiter for an ``approval.responded`` event."""
        request_id = str(message.get("request_id") or "")
        decision = str(message.get("decision") or TIMEOUT)

        with self._lock:
            pending = self._pending.pop(request_id, None)

        if pending is None:
            logger.debug(
                "[clawtalk] approval response for unknown/expired request: %s",
                request_id,
            )
            return

        pending.decision = decision
        pending.event.set()

    def cleanup_pending(self) -> None:
        """Resolve every waiter as ``timeout``.

        Called on disconnect: the decision can no longer reach us, and a
        caller blocked on a phone call must not hang for its full timeout.
        """
        with self._lock:
            pending, self._pending = self._pending, {}

        for request_id, entry in pending.items():
            entry.decision = TIMEOUT
            entry.event.set()
            logger.debug("[clawtalk] cleaned up pending approval: %s", request_id)

    # -- internals ---------------------------------------------------------

    def _wait_for_decision(self, request_id: str, timeout_s: int) -> ApprovalDecision:
        entry = _Pending(event=threading.Event())
        with self._lock:
            self._pending[request_id] = entry

        if not entry.event.wait(timeout=timeout_s):
            with self._lock:
                self._pending.pop(request_id, None)
            return TIMEOUT

        return entry.decision or TIMEOUT
