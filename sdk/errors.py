"""HTTP/API error type for :class:`~clawtalk.sdk.client.ClawTalkClient`.

Plugin-level errors (``ToolError``, ``WebSocketError``, ...) live in
``clawtalk.errors``; this module stays free of plugin imports so the SDK can
be lifted out and reused on its own.
"""

from __future__ import annotations

import json

__all__ = ["ApiError"]

_MAX_RAW_BODY_IN_MESSAGE = 200


class ApiError(Exception):
    """A non-2xx (or unparseable) response from the ClawTalk REST API."""

    def __init__(
        self,
        status_code: int,
        message: str,
        response_body: str | None = None,
    ) -> None:
        server_code: str | None = None
        server_message: str | None = None

        if response_body:
            try:
                parsed = json.loads(response_body)
            except (ValueError, TypeError):
                # Not JSON - fall back to the raw body when it is short enough
                # to be useful rather than noise.
                if len(response_body) < _MAX_RAW_BODY_IN_MESSAGE:
                    server_message = response_body
            else:
                err = parsed.get("error") if isinstance(parsed, dict) else None
                if not isinstance(err, dict):
                    err = parsed if isinstance(parsed, dict) else {}
                server_code = err.get("code") or None
                server_message = err.get("message") or err.get("detail") or None

        full_message = f"{message} - {server_message}" if server_message else message
        super().__init__(full_message)

        self.status_code = status_code
        self.response_body = response_body
        self.server_code = server_code
        self.server_message = server_message

    # -- convenience constructors ------------------------------------------

    @classmethod
    def unauthorized(cls, message: str = "Invalid or expired API key") -> ApiError:
        return cls(401, message)

    @classmethod
    def forbidden(cls, message: str = "Insufficient permissions") -> ApiError:
        return cls(403, message)

    @classmethod
    def not_found(cls, resource: str) -> ApiError:
        return cls(404, f"{resource} not found")

    @classmethod
    def rate_limited(cls, retry_after: int | None = None) -> ApiError:
        message = (
            f"Rate limited. Retry after {retry_after}s"
            if retry_after is not None
            else "Rate limited. Try again later"
        )
        return cls(429, message, str(retry_after) if retry_after is not None else None)

    @classmethod
    def server_error(cls, message: str = "ClawTalk server error") -> ApiError:
        return cls(500, message)
