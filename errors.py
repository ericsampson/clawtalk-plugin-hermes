"""Structured error types for the ClawTalk Hermes plugin.

Every error carries a machine-readable ``code`` plus optional ``details``.
Services raise these; tool handlers catch them and render an error JSON
envelope for the model.
"""

from __future__ import annotations

from typing import Any

from .sdk.errors import ApiError

__all__ = [
    "ApiError",
    "ClawTalkError",
    "ConfigError",
    "ToolError",
    "WebSocketError",
]


class ClawTalkError(Exception):
    """Base class for every plugin-level failure."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, Any] = dict(details or {})


class WebSocketError(ClawTalkError):
    """A failure on the persistent ClawTalk WebSocket."""

    @classmethod
    def auth_failed(cls, reason: str) -> WebSocketError:
        return cls("WS_AUTH_FAILED", f"Authentication failed: {reason}")

    @classmethod
    def disconnected(cls) -> WebSocketError:
        return cls("WS_DISCONNECTED", "WebSocket is not connected")

    @classmethod
    def duplicate_client(cls) -> WebSocketError:
        return cls(
            "WS_DUPLICATE_CLIENT",
            "Another client is already connected. Only one connection per account is allowed.",
        )

    @classmethod
    def send_failed(cls, reason: str) -> WebSocketError:
        return cls("WS_SEND_FAILED", f"Failed to send message: {reason}")


class ConfigError(ClawTalkError):
    """Invalid or missing plugin configuration."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("CONFIG_INVALID", message, details)

    @classmethod
    def missing_field(cls, field: str) -> ConfigError:
        return cls(f"Required config field missing: {field}", {"field": field})


class ToolError(ClawTalkError):
    """A tool-facing failure, phrased so the model can act on it."""

    def __init__(self, tool: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("TOOL_ERROR", f"[{tool}] {message}", {"tool": tool, **(details or {})})
        self.tool = tool

    @classmethod
    def from_exception(cls, tool: str, exc: BaseException) -> ToolError:
        """Normalize any exception into a ToolError with a fix hint when known."""
        if isinstance(exc, ToolError):
            return exc
        if isinstance(exc, ApiError):
            hint = _fix_hint(exc.server_code, exc.server_message)
            message = f"{exc}. Fix: {hint}" if hint else str(exc)
            return cls(
                tool,
                message,
                {"status_code": exc.status_code, "server_code": exc.server_code},
            )
        if isinstance(exc, ClawTalkError):
            return cls(tool, exc.message, {"code": exc.code})
        return cls(tool, str(exc) or exc.__class__.__name__)


def _fix_hint(code: str | None, message: str | None) -> str | None:
    """Translate a known server error code into an actionable next step."""
    if not code and not message:
        return None
    code = code or ""
    text = (message or "").lower()

    if code == "step_not_found" or ("step" in text and "not found" in text):
        return "Use clawtalk_mission_get_plan to list valid step IDs for this mission."
    if code == "missing_field":
        return "Check the tool parameters and provide all required fields."
    if code == "not_found" and "assistant" in text:
        return 'Use clawtalk_assistants (action: "list") to find valid assistant IDs.'
    if code == "not_found" and "mission" in text:
        return "Use clawtalk_mission_list to find valid mission slugs."
    if code == "quota_exceeded":
        return "The user has hit their plan limit for this resource. Inform them of the quota."
    return None
