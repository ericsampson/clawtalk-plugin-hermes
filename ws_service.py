"""Persistent WebSocket connection to the ClawTalk server.

Owns authentication, ping/pong keepalive, exponential-backoff reconnect, and
typed event dispatch. One instance per gateway process, created and torn down
by :class:`~clawtalk.adapter.ClawTalkAdapter`.

The connection is opened *outbound*, so no inbound port or tunnel is needed.

Two conditions stop the service permanently instead of reconnecting:

* ``auth_error`` - the API key is wrong; retrying would just spam the server.
* close code 4000 - another client holds the single allowed connection for
  this account; reconnecting would fight it for the slot.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from . import wire
from .config import ClawTalkConfig
from .errors import WebSocketError
from .version import __version__
from .ws_logger import WsLogger

__all__ = ["WebSocketService"]

logger = logging.getLogger(__name__)

RECONNECT_MIN_S = 5.0
RECONNECT_MAX_S = 180.0
PING_INTERVAL_S = 30.0
PONG_TIMEOUT_S = 10.0
AUTH_TIMEOUT_S = 20.0
CONNECT_TIMEOUT_S = 30.0

#: Non-event frames the read loop dispatches by ``type`` rather than ``event``.
_CONTROL_EVENTS = ("connected", "disconnected", "request_logs")

Handler = Callable[..., Any | Awaitable[Any]]


def _now() -> datetime:
    return datetime.now(UTC)


class WebSocketService:
    """Long-lived, self-healing client for the ClawTalk event socket."""

    def __init__(
        self,
        config: ClawTalkConfig,
        ws_log: WsLogger | None = None,
    ) -> None:
        self._config = config
        self._ws_log = ws_log
        self._handlers: dict[str, list[Handler]] = {}

        self._ws: Any = None
        self._session: Any = None
        self._authenticated = False
        self._first_connect = True
        self._stopped = False
        #: Set when auth fails or we are evicted as a duplicate client.
        self._fatal_error: str | None = None

        self._runner: asyncio.Task | None = None
        self._ping_task: asyncio.Task | None = None
        self._background: set[asyncio.Task] = set()

        self._reconnect_attempts = 0
        self._last_ping_at: datetime | None = None
        self._last_pong_at: datetime | None = None

    # -- introspection -----------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed and self._authenticated

    @property
    def version(self) -> str:
        return __version__

    @property
    def last_ping(self) -> datetime | None:
        return self._last_ping_at

    @property
    def last_pong(self) -> datetime | None:
        return self._last_pong_at

    @property
    def fatal_error(self) -> str | None:
        """Why the service gave up, or ``None`` while it is still trying."""
        return self._fatal_error

    # -- event registration ------------------------------------------------

    def on(self, event: str, handler: Handler) -> None:
        """Subscribe *handler* to an inbound event name.

        Accepts any name in :data:`clawtalk.wire.EVENTS` plus the control
        pseudo-events ``connected``, ``disconnected``, and ``request_logs``.
        Handlers may be sync or async; exceptions are logged, never raised
        into the read loop.
        """
        if event not in wire.EVENTS and event not in _CONTROL_EVENTS:
            raise ValueError(f"Unknown ClawTalk event: {event!r}")
        self._handlers.setdefault(event, []).append(handler)

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Start the connect/reconnect loop. Returns once it is scheduled."""
        if self._runner is not None and not self._runner.done():
            return
        self._stopped = False
        self._fatal_error = None
        self._runner = asyncio.create_task(self._run(), name="clawtalk:ws")

    async def stop(self) -> None:
        """Close the socket and cancel every task this service owns."""
        self._stopped = True
        self._cancel_ping()

        if self._runner is not None:
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner
            self._runner = None

        for task in list(self._background):
            task.cancel()
        self._background.clear()

        await self._close_socket()
        self._authenticated = False

    async def send(self, frame: dict[str, Any]) -> None:
        """Send one frame. Raises :class:`WebSocketError` when disconnected."""
        ws = self._ws
        if ws is None or ws.closed:
            raise WebSocketError.disconnected()
        try:
            if self._ws_log is not None:
                self._ws_log.outbound(frame)
            await ws.send_str(json.dumps(frame))
        except Exception as exc:
            raise WebSocketError.send_failed(str(exc)) from exc

    async def try_send(self, frame: dict[str, Any]) -> bool:
        """Send one frame, logging and swallowing failures. Returns success."""
        try:
            await self.send(frame)
            return True
        except WebSocketError as exc:
            logger.warning("[clawtalk] send failed (%s): %s", frame.get("type"), exc)
            return False

    # -- connect / reconnect loop -----------------------------------------

    async def _run(self) -> None:
        while not self._stopped:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - loop must survive anything
                logger.warning("[clawtalk] WebSocket error: %s", exc)
                self._log_lifecycle("error", str(exc))

            if self._stopped or self._fatal_error:
                return

            delay = self._next_backoff()
            logger.info(
                "[clawtalk] reconnecting in %ds (attempt %d)",
                round(delay),
                self._reconnect_attempts,
            )
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise

    def _next_backoff(self) -> float:
        """Exponential backoff with jitter, capped at :data:`RECONNECT_MAX_S`."""
        self._reconnect_attempts += 1
        base = min(RECONNECT_MIN_S * (2 ** (self._reconnect_attempts - 1)), RECONNECT_MAX_S)
        # Jitter keeps a fleet of gateways from retrying in lockstep after a
        # server-side outage.
        return base * random.uniform(0.8, 1.2)

    async def _connect_once(self) -> None:
        import aiohttp

        url = self._config.ws_url
        logger.info("[clawtalk] connecting to %s", url)

        # The socket is meant to stay open indefinitely, so bound only the
        # connect phase - a total timeout would tear down a healthy session.
        self._session = aiohttp.ClientSession(
            trust_env=True,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=CONNECT_TIMEOUT_S),
        )
        try:
            # autoping=False: we drive the keepalive ourselves so ``clawtalk
            # doctor`` and clawtalk_status can report real ping/pong times.
            # No ``timeout=`` here: aiohttp deprecated the float form, and the
            # session timeout above already covers connect.
            self._ws = await self._session.ws_connect(url, autoping=False)
            self._log_lifecycle("connected", url)
            logger.info("[clawtalk] connected, authenticating...")

            await self._send_auth()
            await self._await_auth()
            await self._read_loop()
        finally:
            self._cancel_ping()
            await self._close_socket()
            self._authenticated = False

    async def _close_socket(self) -> None:
        ws, self._ws = self._ws, None
        session, self._session = self._session, None
        if ws is not None and not ws.closed:
            with contextlib.suppress(Exception):
                await ws.close(code=1000, message=b"Plugin shutdown")
        if session is not None and not session.closed:
            with contextlib.suppress(Exception):
                await session.close()

    # -- auth --------------------------------------------------------------

    async def _send_auth(self) -> None:
        cfg = self._config
        frame = wire.auth(
            api_key=cfg.api_key,
            client_version=__version__,
            # Only send names the user actually customised, so a default
            # install is not auto-named server-side.
            owner_name=cfg.owner_name if cfg.owner_name != "there" else None,
            agent_name=cfg.agent_name if cfg.agent_name != "ClawTalk" else None,
        )
        if self._ws_log is not None:
            self._ws_log.outbound(frame)
        await self._ws.send_str(json.dumps(frame))

    async def _await_auth(self) -> None:
        """Block until ``auth_ok``, or raise on ``auth_error``/timeout."""
        deadline = asyncio.get_running_loop().time() + AUTH_TIMEOUT_S
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise WebSocketError("WS_AUTH_TIMEOUT", "Timed out waiting for auth_ok")

            message = await self._receive(timeout=remaining)
            if message is None:
                # The server can evict us as a duplicate client before it ever
                # answers the auth frame, so the terminal-close check has to
                # run on this path too - otherwise we would reconnect forever,
                # fighting the other client for the single allowed slot.
                self._check_terminal_close()
                raise WebSocketError("WS_CLOSED", "Connection closed during auth")

            kind = message.get("type")
            if kind == "auth_ok":
                self._on_auth_ok()
                return
            if kind == "auth_error":
                error = WebSocketError.auth_failed(message.get("message", "unknown"))
                # A rejected key will be rejected again - stop for good and
                # surface the reason through `hermes gateway status`.
                self._fatal_error = error.message
                self._stopped = True
                logger.error("[clawtalk] %s", error.message)
                self._log_lifecycle("auth_error", error.message)
                raise error
            # Anything else this early is server chatter; keep waiting.

    def _on_auth_ok(self) -> None:
        self._authenticated = True
        self._reconnect_attempts = 0
        self._last_pong_at = _now()

        logger.info("[clawtalk] authenticated (v%s)", __version__)
        self._log_lifecycle("authenticated", f"v{__version__}")

        self._start_ping()

        if not self._first_connect:
            # Tells the server this is a resumed session so it can replay or
            # re-key state rather than treating us as a fresh install.
            self._spawn(self.try_send(wire.client_restart(__version__)))
        self._first_connect = False

        self._emit("connected")

    # -- read loop ---------------------------------------------------------

    async def _receive(self, timeout: float | None = None) -> dict[str, Any] | None:
        """Receive one decoded JSON frame, transparently handling ping/pong.

        Returns ``None`` when the socket closes.
        """
        import aiohttp

        while True:
            message = await self._ws.receive(timeout=timeout)

            if message.type == aiohttp.WSMsgType.TEXT:
                try:
                    decoded = json.loads(message.data)
                except ValueError:
                    logger.warning("[clawtalk] failed to parse WebSocket message")
                    continue
                if not isinstance(decoded, dict):
                    continue
                if self._ws_log is not None:
                    self._ws_log.inbound(decoded)
                return decoded

            if message.type == aiohttp.WSMsgType.PING:
                with contextlib.suppress(Exception):
                    await self._ws.pong(message.data)
                continue

            if message.type == aiohttp.WSMsgType.PONG:
                self._last_pong_at = _now()
                continue

            if message.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            ):
                return None

            # BINARY and anything else: ClawTalk is JSON-only, so ignore.

    async def _read_loop(self) -> None:
        while not self._stopped:
            message = await self._receive()
            if message is None:
                break

            kind = message.get("type")
            if kind == "event":
                self._dispatch_event(message)
            elif kind == "request_logs":
                self._emit("request_logs", message.get("request_id", ""))
            elif kind == "auth_error":
                self._fatal_error = str(message.get("message", "auth error"))
                self._stopped = True
                break

        await self._handle_close()

    async def _handle_close(self) -> None:
        ws = self._ws
        code = getattr(ws, "close_code", None) if ws is not None else None
        reason = "unknown"

        logger.info("[clawtalk] disconnected: code=%s reason=%s", code, reason)
        self._log_lifecycle("disconnected", f"code={code} reason={reason}")
        self._authenticated = False
        self._cancel_ping()
        self._emit("disconnected", code, reason)

        self._check_terminal_close()

    def _check_terminal_close(self) -> None:
        """Stop for good when the close code means retrying cannot help."""
        ws = self._ws
        code = getattr(ws, "close_code", None) if ws is not None else None
        if code != wire.DUPLICATE_CLIENT_CODE:
            return

        error = WebSocketError.duplicate_client()
        logger.error("[clawtalk] %s", error.message)
        self._log_lifecycle("duplicate_client", error.message)
        self._fatal_error = error.message
        self._stopped = True

    def _dispatch_event(self, message: dict[str, Any]) -> None:
        event = message.get("event")
        if not isinstance(event, str):
            return
        if event not in self._handlers:
            logger.debug("[clawtalk] unhandled event: %s", event)
            return
        self._emit(event, message)

    # -- ping/pong ---------------------------------------------------------

    def _start_ping(self) -> None:
        self._cancel_ping()
        self._ping_task = asyncio.create_task(self._ping_loop(), name="clawtalk:ws-ping")

    def _cancel_ping(self) -> None:
        if self._ping_task is not None:
            self._ping_task.cancel()
            self._ping_task = None

    async def _ping_loop(self) -> None:
        """Ping every 30s; tear the socket down if no pong arrives in 10s."""
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL_S)
                ws = self._ws
                if ws is None or ws.closed:
                    return

                self._last_ping_at = _now()
                sent_at = self._last_ping_at
                with contextlib.suppress(Exception):
                    await ws.ping()

                await asyncio.sleep(PONG_TIMEOUT_S)
                pong = self._last_pong_at
                if pong is None or pong < sent_at:
                    logger.warning("[clawtalk] pong timeout, closing connection")
                    self._log_lifecycle("pong_timeout")
                    with contextlib.suppress(Exception):
                        await ws.close()
                    return
        except asyncio.CancelledError:
            raise

    # -- helpers -----------------------------------------------------------

    def _emit(self, event: str, *args: Any) -> None:
        for handler in self._handlers.get(event, ()):
            try:
                result = handler(*args)
            except Exception:
                logger.exception("[clawtalk] handler for %s raised", event)
                continue
            if inspect.isawaitable(result):
                self._spawn(result)

    def _spawn(self, coro: Awaitable[Any]) -> None:
        """Run a coroutine detached, keeping a strong reference to the task."""
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        task.add_done_callback(_log_task_exception)

    def _log_lifecycle(self, event: str, detail: str | None = None) -> None:
        if self._ws_log is not None:
            self._ws_log.lifecycle(event, detail)


def _log_task_exception(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("[clawtalk] background task failed: %s", exc)
