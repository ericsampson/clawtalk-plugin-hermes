"""``hermes clawtalk ...`` - operator commands.

    hermes clawtalk doctor    verify config, credentials, and server health
    hermes clawtalk logs      tail the WebSocket traffic log
    hermes clawtalk status    one-line connection summary

These run outside the gateway process, so they talk to the ClawTalk server
directly over REST rather than to a live adapter. ``doctor`` in particular is
meant to work when the gateway is *not* running - that is usually why someone
is running it.
"""

from __future__ import annotations

import json
import os
import sys
import time
from argparse import ArgumentParser, Namespace
from pathlib import Path

from .runtime import get_runtime
from .version import __version__

__all__ = ["handle_clawtalk_cli", "setup_clawtalk_cli"]

DOCS_URL = "https://clawdtalk.com/docs/plugin"
SERVER_PATTERN = ("clawtalk", "clawdtalk")

GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
RED = "\033[0;31m"
DIM = "\033[2m"
BOLD = "\033[1m"
NC = "\033[0m"

STATUS_ICON = {"pass": f"{GREEN}v{NC}", "warn": f"{YELLOW}!{NC}", "fail": f"{RED}x{NC}"}


def _color(text: str) -> str:
    """Strip ANSI when stdout is not a terminal, so logs stay readable."""
    if sys.stdout.isatty():
        return text
    for code in (GREEN, YELLOW, RED, DIM, BOLD, NC):
        text = text.replace(code, "")
    return text


def _out(text: str = "") -> None:
    print(_color(text))


def _mask_key(key: str) -> str:
    if len(key) < 12:
        return "****"
    return f"{key[:4]}...{key[-4:]}"


def validate_server_url(url: str) -> str | None:
    """Reject anything that is not an HTTPS ClawTalk host.

    The API key travels in every request, so a mistyped or attacker-supplied
    server URL is a credential-exfiltration path. Checking the *hostname*
    (not the whole URL) is what stops ``https://evil.example/?clawtalk``.
    """
    from urllib.parse import urlparse

    if not url.startswith("https://"):
        # localhost over http is a legitimate development setup.
        if url.startswith("http://127.0.0.1") or url.startswith("http://localhost"):
            return None
        return "Server URL must use HTTPS."

    try:
        hostname = (urlparse(url).hostname or "").lower()
    except ValueError:
        return "Invalid URL format."

    if not any(marker in hostname for marker in SERVER_PATTERN):
        return "Server hostname must contain 'clawtalk' or 'clawdtalk'."
    return None


# -- argparse wiring -------------------------------------------------------


def setup_clawtalk_cli(subparser: ArgumentParser) -> None:
    """Attach the ``clawtalk`` subcommands."""
    subparser.epilog = f"Docs: {DOCS_URL}"
    subs = subparser.add_subparsers(dest="clawtalk_command")

    subs.add_parser("doctor", help="Verify ClawTalk configuration and connectivity")
    subs.add_parser("status", help="Show a one-line ClawTalk connection summary")

    logs = subs.add_parser("logs", help="Tail the ClawTalk WebSocket log")
    logs.add_argument(
        "--since", type=int, default=50, help="Print the last N lines first (default 50)"
    )
    logs.add_argument(
        "--poll", type=float, default=0.25, help="Poll interval in seconds (default 0.25)"
    )
    logs.add_argument(
        "--no-follow", action="store_true", help="Print and exit instead of following"
    )

    subparser.set_defaults(func=handle_clawtalk_cli)


def handle_clawtalk_cli(args: Namespace) -> int:
    command = getattr(args, "clawtalk_command", None)
    if command == "doctor":
        return _cmd_doctor()
    if command == "logs":
        return _cmd_logs(args)
    if command == "status":
        return _cmd_status()

    _out("Usage: hermes clawtalk <doctor|logs|status>")
    _out(f"Docs: {DOCS_URL}")
    return 1


# -- commands --------------------------------------------------------------


def _cmd_doctor() -> int:
    rt = get_runtime()
    config = rt.config

    _out()
    _out(f"{BOLD}ClawTalk Doctor{NC}")
    _out()
    _out(f"  Version:  {__version__}")
    _out(f"  API Key:  {_mask_key(config.api_key) if config.api_key else f'{RED}not set{NC}'}")
    _out(f"  Server:   {config.server}")
    _out(f"  Data dir: {rt.data_dir}")

    if not config.api_key:
        _out(f"  Health:   {RED}NO API KEY{NC}")
        _out()
        _out("  Set it with one of:")
        _out(f"    {BOLD}export CLAWTALK_API_KEY=...{NC}")
        _out(f"    {BOLD}gateway.platforms.clawtalk.extra.api_key{NC} in config.yaml")
        _out()
        return 1

    server_error = validate_server_url(config.server)
    if server_error:
        _out(f"  Health:   {RED}UNSUPPORTED_SERVER{NC}")
        _out()
        _out(f"  {server_error}")
        _out()
        return 1

    report = rt.doctor.run_all()

    _out()
    _out(f"  {BOLD}Local{NC}")
    for check in report.local:
        icon = STATUS_ICON.get(check.status, "?")
        detail = f" {DIM}{check.detail}{NC}" if check.detail else ""
        _out(f"  {icon} {check.id}{detail}")

    for label, key in (
        ("Critical", "critical"),
        ("Warnings", "warnings"),
        ("Recommended", "recommended"),
        ("Infrastructure", "infra"),
    ):
        section = report.server.get(key)
        checks = (section or {}).get("checks") or []
        if not checks:
            continue
        _out()
        _out(f"  {BOLD}{label}{NC}")
        for check in checks:
            icon = STATUS_ICON.get(str(check.get("status")), "?")
            detail = f" {DIM}{check.get('detail')}{NC}" if check.get("detail") else ""
            _out(f"  {icon} {check.get('id')}{detail}")

        bot_check = next((c for c in checks if c.get("id") == "bot_connected"), None)
        if bot_check and bot_check.get("status") != "pass":
            _out()
            _out(f"  {YELLOW}The Hermes gateway owns the ClawTalk WebSocket.{NC}")
            _out("  1. Check the gateway is running: hermes gateway status")
            _out("  2. Confirm the platform is enabled: gateway.platforms.clawtalk.enabled")
            _out("  3. Review `hermes clawtalk logs` for auth or connection errors.")

    tail = rt.ws_log.read_recent_lines(5)
    if tail:
        _out()
        _out(f"  {BOLD}WebSocket Log{NC}")
        for line in tail:
            _out(f"  {DIM}{line}{NC}")

    _out()
    return 0 if report.healthy else 1


def _cmd_status() -> int:
    rt = get_runtime()
    if not rt.config.api_key:
        _out(f"{RED}ClawTalk: no API key configured{NC}")
        return 1

    try:
        me = rt.client.user.me()
    except Exception as exc:  # noqa: BLE001 - the message is the whole point
        _out(f"{RED}ClawTalk: unreachable ({exc}){NC}")
        return 1

    number = me.get("dedicated_number") or me.get("system_number") or "none"
    _out(
        f"{GREEN}ClawTalk{NC} v{__version__} | {rt.config.server} | "
        f"{me.get('email') or me.get('user_id')} | "
        f"plan {me.get('effective_tier') or 'unknown'} | number {number}"
    )
    return 0


def _cmd_logs(args: Namespace) -> int:
    log_path = _find_ws_log()
    if log_path is None:
        _out(f"{RED}No ClawTalk WebSocket log found.{NC}")
        _out("The log appears once the gateway connects. Start it with: hermes gateway")
        return 1

    _out(f"{DIM}Tailing {log_path}{NC}")

    try:
        content = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _out(f"{RED}Could not read {log_path}: {exc}{NC}")
        return 1

    lines = [line for line in content.split("\n") if line]
    for line in lines[-max(0, args.since) :] if args.since else lines:
        print(line)

    if args.no_follow:
        return 0

    offset = len(content.encode("utf-8"))
    poll = max(0.05, float(args.poll))

    try:
        while True:
            try:
                size = log_path.stat().st_size
                if size < offset:
                    # Rotated out from under us - start over from the top.
                    offset = 0
                if size > offset:
                    with open(log_path, "rb") as handle:
                        handle.seek(offset)
                        chunk = handle.read(size - offset)
                    offset = size
                    for line in chunk.decode("utf-8", errors="replace").split("\n"):
                        if line:
                            print(line)
            except OSError:
                pass
            time.sleep(poll)
    except KeyboardInterrupt:
        return 0


def _find_ws_log() -> Path | None:
    """Return the most recently written candidate log file."""
    from .runtime import resolve_data_dir

    hermes_home = Path(
        os.getenv("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
    )
    candidates = [
        resolve_data_dir() / "ws.log",
        hermes_home / "plugin-data" / "clawtalk" / "ws.log",
        hermes_home / "ws.log",
    ]

    best: tuple[float, Path] | None = None
    for candidate in candidates:
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, candidate)
    return best[1] if best else None


def render_status_json() -> str:
    """Machine-readable status, shared with the ``/clawtalk`` slash command."""
    rt = get_runtime()
    return json.dumps(
        {
            "version": __version__,
            "server": rt.config.server,
            "connected": rt.ws_connected,
            "missions_enabled": rt.config.missions.enabled,
            "data_dir": str(rt.data_dir),
        },
        indent=2,
    )
