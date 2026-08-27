"""ClawTalkClient - Stripe-style namespaced SDK for the ClawTalk REST API.

Usage::

    client = ClawTalkClient(api_key=key, server="https://clawdtalk.com")
    client.calls.initiate(to="+353851234567")
    client.missions.runs.create(mission_id, {"original_request": "..."})

The client is **synchronous** and built on ``urllib.request`` so it has no
third-party dependencies and is safe to call from Hermes tool handlers no
matter which thread or event loop they run on. Async callers (the gateway
adapter, the mission observer) should wrap calls in ``asyncio.to_thread``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import urllib.error
import urllib.request
from typing import Any

from .errors import ApiError
from .namespaces import (
    ApprovalsNamespace,
    AssistantsNamespace,
    CallsNamespace,
    DoctorNamespace,
    InsightsNamespace,
    MissionsNamespace,
    NumbersNamespace,
    SmsNamespace,
    UserNamespace,
    VoicesNamespace,
)

__all__ = ["ClawTalkClient"]

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 30.0


class ClawTalkClient:
    """Thin, typed-ish REST client with one namespace per server resource."""

    def __init__(
        self,
        api_key: str,
        server: str,
        *,
        client_version: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        self._base_url = server.rstrip("/")
        self._timeout_s = timeout_s
        self._opener = opener or urllib.request.build_opener()
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if client_version:
            self._headers["X-Client-Version"] = client_version

        self.calls = CallsNamespace(self.request)
        self.sms = SmsNamespace(self.request)
        self.missions = MissionsNamespace(self.request)
        self.assistants = AssistantsNamespace(self.request)
        self.approvals = ApprovalsNamespace(self.request)
        self.user = UserNamespace(self.request)
        self.numbers = NumbersNamespace(self.request)
        self.insights = InsightsNamespace(self.request)
        self.doctor = DoctorNamespace(self.request)
        self.voices = VoicesNamespace(self.request)

    @property
    def base_url(self) -> str:
        return self._base_url

    # -- transport ---------------------------------------------------------

    def request(self, method: str, endpoint: str, body: Any = None) -> Any:
        """Issue one HTTP request and return the decoded JSON body.

        An empty response body decodes to ``{}`` so callers can use
        ``result.get(...)`` unconditionally.

        Raises:
            ApiError: on timeout, transport failure, non-2xx status, or a
                body that is not valid JSON.
        """
        url = f"{self._base_url}{endpoint}"
        payload = None if body is None else json.dumps(body).encode("utf-8")

        logger.debug("[clawtalk] %s %s", method, endpoint)
        request = urllib.request.Request(
            url, data=payload, headers=self._headers, method=method
        )

        try:
            with self._opener.open(request, timeout=self._timeout_s) as response:
                status = response.getcode()
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = ""
            # pragma: no cover - the body is normally readable, but urllib
            # closes it in some error paths and there is nothing to recover.
            with contextlib.suppress(Exception):
                raw = exc.read().decode("utf-8", errors="replace")
            logger.warning("[clawtalk] API %s: %s %s", exc.code, method, endpoint)
            raise ApiError(
                exc.code, f"{method} {endpoint} failed: {exc.code}", raw
            ) from exc
        except TimeoutError as exc:
            raise ApiError(408, f"Request timed out: {method} {endpoint}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError):
                raise ApiError(408, f"Request timed out: {method} {endpoint}") from exc
            raise ApiError(
                0, f"Network error: {method} {endpoint} - {reason}"
            ) from exc
        except OSError as exc:
            raise ApiError(0, f"Network error: {method} {endpoint} - {exc}") from exc

        if status is not None and status >= 400:
            raise ApiError(status, f"{method} {endpoint} failed: {status}", raw)

        if not raw.strip():
            return {}

        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ApiError(0, f"Invalid JSON response: {method} {endpoint}") from exc
