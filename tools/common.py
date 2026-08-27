"""Shared plumbing for the ClawTalk agent tools.

Hermes tool handlers take ``(args: dict, **kwargs)`` and return a JSON
*string* for both success and failure - a raised exception or a returned dict
is a bug. :func:`tool` enforces that contract in one place so no individual
tool has to remember it.
"""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

from ..errors import ToolError
from ..runtime import ClawTalkRuntime, get_runtime

__all__ = [
    "TOOLSET",
    "fail",
    "ok",
    "optional_json",
    "require",
    "runtime",
    "tool",
]

logger = logging.getLogger(__name__)

#: Toolset every ClawTalk tool registers under, so operators can enable or
#: disable the whole surface with one entry in ``tools``.
TOOLSET = "clawtalk"


def runtime() -> ClawTalkRuntime:
    """The shared service graph. Built on first use."""
    return get_runtime()


def ok(payload: Mapping[str, Any], message: str | None = None) -> str:
    """Render a success envelope."""
    body: dict[str, Any] = {"success": True, **dict(payload)}
    if message is not None:
        body["message"] = message
    return json.dumps(body, indent=2, default=str)


def fail(tool_name: str, exc: BaseException) -> str:
    """Render a failure envelope, with a fix hint when the server gave one."""
    error = ToolError.from_exception(tool_name, exc)
    logger.warning("[clawtalk] %s failed: %s", tool_name, error.message)
    body: dict[str, Any] = {"success": False, "error": error.message}
    details = {k: v for k, v in error.details.items() if k != "tool" and v is not None}
    if details:
        body["details"] = details
    return json.dumps(body, indent=2, default=str)


def tool(name: str) -> Callable[[Callable[..., Any]], Callable[..., str]]:
    """Wrap a handler so it always returns a JSON string and never raises."""

    def decorate(func: Callable[..., Any]) -> Callable[..., str]:
        @functools.wraps(func)
        def wrapper(args: Mapping[str, Any] | None = None, **kwargs: Any) -> str:
            try:
                return func(dict(args or {}), **kwargs)
            except Exception as exc:  # noqa: BLE001 - the contract is "never raise"
                return fail(name, exc)

        wrapper.clawtalk_tool_name = name  # type: ignore[attr-defined]
        return wrapper

    return decorate


def require(args: Mapping[str, Any], *names: str) -> tuple:
    """Return the named args, raising a clear error for missing ones."""
    missing = [name for name in names if not args.get(name)]
    if missing:
        raise ValueError(f"Missing required parameter(s): {', '.join(missing)}")
    return tuple(args[name] for name in names)


def optional_json(raw: Any, fallback: Any = None) -> Any:
    """Parse a JSON-string parameter, falling back when it is absent or bad.

    Several tools take structured input as a JSON string because that is the
    shape models reliably produce for nested data.
    """
    if raw in (None, ""):
        return fallback
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return fallback
