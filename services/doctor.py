"""Hybrid health checks: local plugin state plus the server's own diagnostics.

Local checks answer "is this gateway wired up correctly"; the four server
categories (critical / warnings / recommended / infra) answer "is the ClawTalk
account wired up correctly".

Everything here is synchronous so ``hermes clawtalk doctor`` can run it
without a live gateway or event loop.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..sdk import ApiError, ClawTalkClient
from ..version import __version__

__all__ = ["DoctorCheck", "DoctorReport", "DoctorService"]

logger = logging.getLogger(__name__)

PASS = "pass"
WARN = "warn"
FAIL = "fail"


@dataclass(frozen=True)
class DoctorCheck:
    """One named health check outcome."""

    id: str
    status: str
    detail: str | None = None
    source: str = "local"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "detail": self.detail,
            "source": self.source,
        }


@dataclass
class DoctorReport:
    """Local checks plus the raw server payload for each category."""

    local: list[DoctorCheck] = field(default_factory=list)
    server: dict[str, dict[str, Any] | None] = field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        """True when no local check failed."""
        return all(check.status != FAIL for check in self.local)

    def to_dict(self) -> dict[str, Any]:
        return {
            "local": [check.to_dict() for check in self.local],
            "server": self.server,
        }


class DoctorService:
    """Runs the local and server-side health checks."""

    def __init__(
        self,
        client: ClawTalkClient,
        *,
        ws: Any = None,
        has_api_key: bool = True,
    ) -> None:
        self._client = client
        self._ws = ws
        self._has_api_key = has_api_key

    def run_all(self) -> DoctorReport:
        return DoctorReport(local=self.run_local(), server=self.run_server())

    # -- local -------------------------------------------------------------

    def run_local(self) -> list[DoctorCheck]:
        return [
            self._check_api_key(),
            self._check_dependencies(),
            self._check_websocket(),
            self._check_client_version(),
            self._check_account(),
        ]

    def _check_api_key(self) -> DoctorCheck:
        if self._has_api_key:
            return DoctorCheck("api_key", PASS, "API key configured")
        return DoctorCheck(
            "api_key",
            FAIL,
            "No API key. Set CLAWTALK_API_KEY or gateway.platforms.clawtalk.extra.api_key",
        )

    def _check_dependencies(self) -> DoctorCheck:
        try:
            import aiohttp  # noqa: F401
        except ImportError:
            return DoctorCheck(
                "dependencies", FAIL, "aiohttp is not installed (pip install aiohttp)"
            )
        return DoctorCheck("dependencies", PASS, "aiohttp available")

    def _check_websocket(self) -> DoctorCheck:
        if self._ws is None:
            return DoctorCheck(
                "ws_connected",
                WARN,
                "No live gateway in this process - run `hermes gateway status` "
                "to check the running gateway",
            )
        if self._ws.is_connected:
            return DoctorCheck("ws_connected", PASS, "WebSocket connected")

        fatal = getattr(self._ws, "fatal_error", None)
        return DoctorCheck(
            "ws_connected",
            FAIL,
            f"WebSocket disconnected: {fatal}" if fatal else "WebSocket disconnected",
        )

    def _check_client_version(self) -> DoctorCheck:
        if __version__ in ("", "0.0.0"):
            return DoctorCheck(
                "client_version", WARN, "Unable to determine client version"
            )
        return DoctorCheck("client_version", PASS, f"Client version: {__version__}")

    def _check_account(self) -> DoctorCheck:
        """Confirm the key actually authenticates, and report the plan."""
        if not self._has_api_key:
            return DoctorCheck("account", FAIL, "No API key to authenticate with")
        try:
            me = self._client.user.me()
        except ApiError as exc:
            if exc.status_code in (401, 403):
                return DoctorCheck(
                    "account",
                    FAIL,
                    "API key rejected. Generate a new one in the ClawTalk portal.",
                )
            return DoctorCheck("account", FAIL, f"GET /v1/me failed: {exc}")
        except Exception as exc:  # noqa: BLE001 - network, DNS, TLS...
            return DoctorCheck("account", FAIL, f"Server unreachable: {exc}")

        email = me.get("email") or me.get("user_id") or "unknown"
        tier = me.get("effective_tier") or me.get("subscription_tier") or "unknown"
        return DoctorCheck("account", PASS, f"Authenticated as {email} (plan: {tier})")

    # -- server ------------------------------------------------------------

    def run_server(self) -> dict[str, dict[str, Any] | None]:
        """Fetch all four server categories, tolerating individual failures."""
        categories: dict[str, Callable[[], dict[str, Any]]] = {
            "critical": self._client.doctor.critical,
            "warnings": self._client.doctor.warnings,
            "recommended": self._client.doctor.recommended,
            "infra": self._client.doctor.infra,
        }

        results: dict[str, dict[str, Any] | None] = {}
        for name, fetch in categories.items():
            try:
                results[name] = fetch()
            except Exception as exc:  # noqa: BLE001 - report the rest anyway
                logger.warning("[clawtalk] doctor %s fetch failed: %s", name, exc)
                results[name] = None
        return results
