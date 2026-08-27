"""End-to-end WebSocket tests against a real local server.

These drive :class:`WebSocketService` over an actual aiohttp socket rather
than a mock, because the parts most likely to break in production - the auth
handshake, ping/pong keepalive, and the two permanent-stop conditions - only
exist at the transport layer.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any

import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402

from clawtalk import wire  # noqa: E402
from clawtalk.config import ClawTalkConfig  # noqa: E402
from clawtalk.errors import WebSocketError  # noqa: E402
from clawtalk.ws_service import WebSocketService  # noqa: E402


class FakeServer:
    """A stand-in ClawTalk event socket."""

    def __init__(self, *, auth_ok: bool = True, close_code: int | None = None) -> None:
        self.auth_ok = auth_ok
        self.close_code = close_code
        #: Frames the client sent us, in order.
        self.received: list[dict[str, Any]] = []
        #: Set once a client has authenticated.
        self.authenticated = asyncio.Event()
        self.runner: web.AppRunner | None = None
        self.port = 0
        self._sockets: list[web.WebSocketResponse] = []

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/ws", self._handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        for socket in self._sockets:
            if not socket.closed:
                await socket.close()
        if self.runner is not None:
            await self.runner.cleanup()

    async def push(self, frame: dict[str, Any]) -> None:
        """Send a frame to every connected client."""
        for socket in self._sockets:
            if not socket.closed:
                await socket.send_str(json.dumps(frame))

    async def _handle(self, request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse(autoping=False)
        await socket.prepare(request)
        self._sockets.append(socket)

        async for message in socket:
            if message.type == aiohttp.WSMsgType.PING:
                await socket.pong(message.data)
                continue
            if message.type != aiohttp.WSMsgType.TEXT:
                continue

            frame = json.loads(message.data)
            self.received.append(frame)

            if frame.get("type") == "auth":
                if self.close_code is not None:
                    await socket.close(code=self.close_code)
                    return socket
                if self.auth_ok:
                    await socket.send_str(json.dumps({"type": "auth_ok"}))
                    self.authenticated.set()
                else:
                    await socket.send_str(
                        json.dumps({"type": "auth_error", "message": "bad key"})
                    )
                    await socket.close()
                    return socket

        return socket


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


async def _serve(**kwargs):
    server = FakeServer(**kwargs)
    url = await server.start()
    return server, url


def run(coro):
    return asyncio.run(coro)


class TestHandshake:
    def test_authenticates_and_reports_connected(self, config):
        async def scenario():
            server, url = await _serve()
            service = WebSocketService(dataclasses.replace(config, server=url))
            connected = asyncio.Event()
            service.on("connected", connected.set)

            await service.start()
            try:
                await asyncio.wait_for(connected.wait(), timeout=5)
                assert service.is_connected is True

                auth = server.received[0]
                assert auth["type"] == "auth"
                assert auth["api_key"] == "ct_test_key"
                # Stock names must not be sent, or the server auto-names the bot.
                assert "owner_name" not in auth
                assert "agent_name" not in auth
            finally:
                await service.stop()
                await server.stop()

        run(scenario())

    def test_sends_custom_names_when_configured(self, config):
        async def scenario():
            server, url = await _serve()
            service = WebSocketService(
                dataclasses.replace(
                    config, server=url, owner_name="Rudra", agent_name="Daisy"
                )
            )
            await service.start()
            try:
                await asyncio.wait_for(server.authenticated.wait(), timeout=5)
                assert server.received[0]["owner_name"] == "Rudra"
                assert server.received[0]["agent_name"] == "Daisy"
            finally:
                await service.stop()
                await server.stop()

        run(scenario())

    def test_auth_error_stops_permanently(self, config):
        async def scenario():
            server, url = await _serve(auth_ok=False)
            service = WebSocketService(dataclasses.replace(config, server=url))

            await service.start()
            try:
                # A rejected key will be rejected again; retrying only spams.
                await _wait_for(lambda: service.fatal_error is not None)
                assert "Authentication failed" in service.fatal_error
                assert service.is_connected is False
            finally:
                await service.stop()
                await server.stop()

        run(scenario())

    def test_duplicate_client_close_stops_permanently(self, config):
        async def scenario():
            server, url = await _serve(close_code=wire.DUPLICATE_CLIENT_CODE)
            service = WebSocketService(dataclasses.replace(config, server=url))

            await service.start()
            try:
                await _wait_for(lambda: service.fatal_error is not None)
                assert "Another client" in service.fatal_error
            finally:
                await service.stop()
                await server.stop()

        run(scenario())


class TestDispatch:
    def test_events_reach_their_handler(self, config):
        async def scenario():
            server, url = await _serve()
            service = WebSocketService(dataclasses.replace(config, server=url))

            seen: list[dict[str, Any]] = []
            service.on(wire.EVENT_SMS_RECEIVED, seen.append)

            await service.start()
            try:
                await asyncio.wait_for(server.authenticated.wait(), timeout=5)
                await server.push(
                    {
                        "type": "event",
                        "event": "sms.received",
                        "from": "+15551234567",
                        "body": "you around?",
                    }
                )
                await _wait_for(lambda: len(seen) == 1)
                assert seen[0]["body"] == "you around?"
            finally:
                await service.stop()
                await server.stop()

        run(scenario())

    def test_async_handlers_are_awaited(self, config):
        async def scenario():
            server, url = await _serve()
            service = WebSocketService(dataclasses.replace(config, server=url))

            done = asyncio.Event()

            async def handler(_message):
                await asyncio.sleep(0)
                done.set()

            service.on(wire.EVENT_DEEP_TOOL_REQUEST, handler)

            await service.start()
            try:
                await asyncio.wait_for(server.authenticated.wait(), timeout=5)
                await server.push(
                    {
                        "type": "event",
                        "event": "deep_tool_request",
                        "call_id": "c1",
                        "request_id": "r1",
                        "query": "check Slack",
                    }
                )
                await asyncio.wait_for(done.wait(), timeout=5)
            finally:
                await service.stop()
                await server.stop()

        run(scenario())

    def test_a_raising_handler_does_not_kill_the_read_loop(self, config):
        async def scenario():
            server, url = await _serve()
            service = WebSocketService(dataclasses.replace(config, server=url))

            survivors: list[Any] = []
            service.on(wire.EVENT_SMS_RECEIVED, lambda _m: (_ for _ in ()).throw(RuntimeError("boom")))
            service.on(wire.EVENT_SMS_RECEIVED, survivors.append)

            await service.start()
            try:
                await asyncio.wait_for(server.authenticated.wait(), timeout=5)
                await server.push({"type": "event", "event": "sms.received", "body": "one"})
                await server.push({"type": "event", "event": "sms.received", "body": "two"})
                await _wait_for(lambda: len(survivors) == 2)
                assert service.is_connected is True
            finally:
                await service.stop()
                await server.stop()

        run(scenario())

    def test_unknown_events_are_ignored(self, config):
        async def scenario():
            server, url = await _serve()
            service = WebSocketService(dataclasses.replace(config, server=url))
            await service.start()
            try:
                await asyncio.wait_for(server.authenticated.wait(), timeout=5)
                await server.push({"type": "event", "event": "something.new"})
                await server.push({"type": "not_a_frame_we_know"})
                await asyncio.sleep(0.1)
                assert service.is_connected is True
            finally:
                await service.stop()
                await server.stop()

        run(scenario())

    def test_malformed_json_is_ignored(self, config):
        async def scenario():
            server, url = await _serve()
            service = WebSocketService(dataclasses.replace(config, server=url))
            await service.start()
            try:
                await asyncio.wait_for(server.authenticated.wait(), timeout=5)
                for socket in server._sockets:
                    await socket.send_str("{not json")
                await asyncio.sleep(0.1)
                assert service.is_connected is True
            finally:
                await service.stop()
                await server.stop()

        run(scenario())


class TestSend:
    def test_frames_reach_the_server(self, config):
        async def scenario():
            server, url = await _serve()
            service = WebSocketService(dataclasses.replace(config, server=url))

            await service.start()
            try:
                await asyncio.wait_for(server.authenticated.wait(), timeout=5)
                await service.send(wire.deep_tool_result("c1", "r1", "Done."))
                await _wait_for(lambda: len(server.received) == 2)

                frame = server.received[1]
                assert frame["type"] == "deep_tool_result"
                assert frame["text"] == "Done."
            finally:
                await service.stop()
                await server.stop()

        run(scenario())

    def test_send_while_disconnected_raises(self, config):
        async def scenario():
            service = WebSocketService(config)
            with pytest.raises(WebSocketError):
                await service.send({"type": "response"})

        run(scenario())

    def test_try_send_swallows_the_failure(self, config):
        async def scenario():
            service = WebSocketService(config)
            assert await service.try_send({"type": "response"}) is False

        run(scenario())


class TestReconnect:
    def test_reconnects_and_announces_the_restart(self, config):
        async def scenario():
            server, url = await _serve()
            service = WebSocketService(dataclasses.replace(config, server=url))

            # Shorten the backoff so the test does not wait five seconds.
            import clawtalk.ws_service as ws_module

            original = ws_module.RECONNECT_MIN_S
            ws_module.RECONNECT_MIN_S = 0.05

            await service.start()
            try:
                await asyncio.wait_for(server.authenticated.wait(), timeout=5)
                server.authenticated.clear()

                # Drop the connection from the server side.
                for socket in list(server._sockets):
                    await socket.close()
                server._sockets.clear()

                await asyncio.wait_for(server.authenticated.wait(), timeout=10)
                # The second session must tell the server this is a resume, not
                # a cold start, so it can re-key rather than re-provision.
                await _wait_for(
                    lambda: any(f.get("type") == "client_restart" for f in server.received)
                )
            finally:
                ws_module.RECONNECT_MIN_S = original
                await service.stop()
                await server.stop()

        run(scenario())

    def test_backoff_grows_and_is_capped(self, config):
        service = WebSocketService(config)
        import clawtalk.ws_service as ws_module

        delays = [service._next_backoff() for _ in range(12)]
        assert delays[0] < delays[3] < delays[6]
        assert all(delay <= ws_module.RECONNECT_MAX_S * 1.2 for delay in delays)


class TestHandlerRegistration:
    def test_rejects_an_unknown_event_name(self, config):
        service = WebSocketService(config)
        with pytest.raises(ValueError, match="Unknown ClawTalk event"):
            service.on("call.exploded", lambda _m: None)

    def test_accepts_every_documented_event(self, config):
        service = WebSocketService(config)
        for event in wire.EVENTS:
            service.on(event, lambda _m: None)
        for control in ("connected", "disconnected", "request_logs"):
            service.on(control, lambda *_a: None)


@pytest.fixture
def config() -> ClawTalkConfig:
    return ClawTalkConfig(api_key="ct_test_key", server="https://clawdtalk.com")
