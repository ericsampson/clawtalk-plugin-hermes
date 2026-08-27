"""Dedicated WebSocket traffic log for ClawTalk.

Writes every inbound/outbound frame and lifecycle transition to a rolling
file separate from the gateway log, with sensitive keys redacted. The server
can ask for a tail of this file over the socket (``request_logs``), and
``hermes clawtalk logs`` tails it locally.

Default path: ``<hermes plugin data dir>/ws.log``.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = ["WsLogger"]

MAX_LOG_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB
ROTATION_CHECK_INTERVAL = 100  # writes between size checks
MAX_REDACT_DEPTH = 10
REDACTED = "[REDACTED]"

SENSITIVE_KEYS = frozenset({"api_key", "apiKey", "authorization", "token", "secret"})

#: Internal plumbing that must never be logged - logging it would feed the
#: log-tail request back into the log on every round trip.
SUPPRESSED_TYPES = frozenset({"request_logs", "logs_response"})


def redact(obj: Any, depth: int = 0) -> Any:
    """Deep-copy *obj*, replacing values of sensitive keys with a marker."""
    if depth > MAX_REDACT_DEPTH:
        return "[max depth]"
    if obj is None or isinstance(obj, str | int | float | bool):
        return obj
    if isinstance(obj, list | tuple):
        return [redact(item, depth + 1) for item in obj]
    if isinstance(obj, dict):
        return {
            key: REDACTED if key in SENSITIVE_KEYS else redact(value, depth + 1)
            for key, value in obj.items()
        }
    return str(obj)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class WsLogger:
    """Append-only, size-rotated log of WebSocket traffic.

    Every method is best-effort: a full disk or a permissions error disables
    logging rather than taking the gateway down with it.
    """

    def __init__(self, log_path: str | os.PathLike[str]) -> None:
        self.path = Path(log_path)
        self._handle = None
        self._write_count = 0
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        """Create parent dirs, rotate if oversized, and open for appending."""
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_if_needed()
                self._open_handle()
            except OSError:
                self._handle = None

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                with contextlib.suppress(OSError):
                    self._handle.close()
                self._handle = None

    # -- writers -----------------------------------------------------------

    def outbound(self, message: Any) -> None:
        """Log a frame we sent (client -> server)."""
        self._write(">>>", message)

    def inbound(self, message: Any) -> None:
        """Log a frame we received (server -> client)."""
        self._write("<<<", message)

    def lifecycle(self, event: str, detail: str | None = None) -> None:
        """Log a connection transition (connected, authenticated, error...)."""
        line = f"{_timestamp()} --- {event}: {detail}" if detail else f"{_timestamp()} --- {event}"
        self._write_line(line)

    # -- readers -----------------------------------------------------------

    def read_recent_lines(self, max_lines: int = 200) -> list[str]:
        """Return the last *max_lines* lines. Safe to call while writing."""
        try:
            content = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        lines = [line for line in content.split("\n") if line]
        return lines[-max_lines:] if max_lines > 0 else lines

    # -- internals ---------------------------------------------------------

    def _open_handle(self) -> None:
        # Deliberately not a context manager: the handle stays open for the
        # life of the gateway so every frame appends without a reopen.
        # close() and the rotation path own its lifecycle.
        self._handle = open(self.path, "a", encoding="utf-8")  # noqa: SIM115

    def _write(self, direction: str, message: Any) -> None:
        if isinstance(message, dict) and message.get("type") in SUPPRESSED_TYPES:
            return
        try:
            payload = json.dumps(redact(message))
        except (TypeError, ValueError):
            payload = str(message)
        self._write_line(f"{_timestamp()} {direction} {payload}")

    def _write_line(self, line: str) -> None:
        with self._lock:
            if self._handle is None:
                return
            try:
                self._handle.write(line + "\n")
                self._handle.flush()
            except (OSError, ValueError):
                # Disk full, closed handle, read-only mount - stop logging
                # rather than propagating into the WebSocket read loop.
                self._handle = None
                return

            self._write_count += 1
            if self._write_count >= ROTATION_CHECK_INTERVAL:
                self._write_count = 0
                if self._rotate_if_needed():
                    try:
                        self._handle.close()
                        self._open_handle()
                    except OSError:
                        self._handle = None

    def _rotate_if_needed(self) -> bool:
        """Rename the log to ``<path>.1`` when oversized. Returns True if rotated."""
        try:
            if self.path.exists() and self.path.stat().st_size > MAX_LOG_SIZE_BYTES:
                self.path.replace(self.path.with_suffix(self.path.suffix + ".1"))
                return True
        except OSError:
            pass
        return False
