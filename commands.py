"""The ``/clawtalk`` in-session slash command.

Deliberately read-only. Anything that places a call, sends a text, or moves a
mission belongs in a tool, where it goes through the normal approval and
redaction path - a slash command bypasses all of that.
"""

from __future__ import annotations

from typing import Any

from .runtime import get_runtime
from .version import __version__

__all__ = ["ARGS_HINT", "handle_clawtalk_command"]

ARGS_HINT = "status|doctor|missions"

USAGE = "Usage: /clawtalk status | /clawtalk doctor | /clawtalk missions"


def handle_clawtalk_command(raw_args: str = "", **_kwargs: Any) -> str:
    """Render a short ClawTalk report for the current session."""
    action = (raw_args or "status").strip().split()[0].lower() if raw_args.strip() else "status"

    if action == "status":
        return _status()
    if action == "doctor":
        return _doctor()
    if action == "missions":
        return _missions()
    return USAGE


def _status() -> str:
    rt = get_runtime()
    ws = rt.ws

    lines = [
        f"ClawTalk v{__version__}",
        f"  server:    {rt.config.server}",
        f"  websocket: {'connected' if rt.ws_connected else 'disconnected'}",
        f"  missions:  {'enabled' if rt.config.missions.enabled else 'disabled'}",
    ]
    if ws is not None and ws.fatal_error:
        lines.append(f"  error:     {ws.fatal_error}")
    if not rt.config.has_api_key:
        lines.append("  api key:   NOT SET (set CLAWTALK_API_KEY)")
    return "\n".join(lines)


def _doctor() -> str:
    report = get_runtime().doctor.run_local()
    icons = {"pass": "[ok]", "warn": "[!]", "fail": "[x]"}
    lines = ["ClawTalk local checks:"]
    lines += [
        f"  {icons.get(check.status, '[?]')} {check.id}"
        + (f" - {check.detail}" if check.detail else "")
        for check in report
    ]
    return "\n".join(lines)


def _missions() -> str:
    missions = get_runtime().missions.list_missions()
    if not missions:
        return "No active ClawTalk missions."

    lines = [f"{len(missions)} active ClawTalk mission(s):"]
    for slug, state in missions:
        phone = state.get("agent_phone") or "no number"
        lines.append(f"  {slug} - {state.get('mission_name') or 'unnamed'} ({phone})")
    return "\n".join(lines)
