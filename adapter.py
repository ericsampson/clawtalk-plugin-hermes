"""ClawTalk gateway platform adapter.

This is where the OpenClaw plugin's architecture is deliberately *not*
transliterated. There, a ``CoreBridge`` reached into OpenClaw's compiled
``extensionAPI.js`` to run embedded agent turns out-of-band. Hermes has no
such seam - and does not need one, because a gateway platform adapter already
provides exactly the missing piece: inbound events become normal agent turns
with the full tool set, memory, and per-chat session isolation, and the
agent's reply comes back through :meth:`ClawTalkAdapter.send`.

So each ClawTalk channel is modelled as a namespaced chat id on one platform:

======================  =========================  =========================
Channel                 chat id                    reply frame
======================  =========================  =========================
Voice deep-tool call    ``call:<call_id>``         ``deep_tool_result`` then
                                                   ``response`` for follow-ups
Walkie-talkie           ``walkie:<session>``       ``walkie_response``
SMS / MMS               ``sms:<digits>``           REST ``POST /v1/messages/send``
Mission                 ``mission:<slug>``         (none - the agent acts via tools)
Call outcome report     ``events:calls``           (none - report only)
======================  =========================  =========================

Because Hermes derives the session key from platform + chat id, that table is
also the session-isolation table: every call, contact, and mission gets its
own conversation, which is what the OpenClaw session-key scheme achieved by
hand.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

from . import wire
from .formatting import clean_text_for_voice, mask_phone, normalize_phone, truncate
from .runtime import get_runtime
from .services.mission_events import MissionEventHandler
from .services.mission_observer import MissionObserver
from .services.voice import CALL_MEMORY_BLURB
from .ws_service import WebSocketService

__all__ = ["ClawTalkAdapter", "check_clawtalk_requirements"]

logger = logging.getLogger(__name__)

PLATFORM_NAME = "clawtalk"

# -- chat-id namespaces ----------------------------------------------------

CHAT_CALL = "call:"
CHAT_WALKIE = "walkie:"
CHAT_SMS = "sms:"
CHAT_MISSION = "mission:"
CHAT_CALL_EVENTS = "events:calls"

DEFAULT_WALKIE_SESSION = "walkie:default"

# -- per-channel turn prompts ---------------------------------------------

VOICE_PREFIX = (
    "[VOICE CALL] Respond concisely for speech. No markdown, no lists, no URLs. "
    "Do NOT request approval - it has already been handled. Just perform the "
    "action directly. "
)

WALKIE_PREFIX = (
    "[WALKIE-TALKIE] Push-to-talk message. Respond concisely for speech "
    "(1-3 sentences). No markdown, no lists, no URLs. "
)

SMS_SYSTEM_PROMPT = """You are replying via SMS. HARD LIMITS:

1. MAX {limit} CHARACTERS (messages over {limit} chars are rejected)
2. PLAIN TEXT ONLY - no markdown, no **bold**, no _italics_, no bullets, no headers, no formatting whatsoever
3. No emojis

Write like a text message: short, direct, conversational. If you can't fit the answer in {limit} chars, give the most useful summary possible."""

CALL_EVENTS_PROMPT = (
    "You are receiving a ClawTalk call outcome report. It is informational. "
    "Only take action if the outcome clearly calls for follow-up; otherwise "
    "acknowledge briefly and stop."
)

WS_LOG_TAIL_LINES = 200


def check_clawtalk_requirements() -> bool:
    """Passive dependency probe. Must not install anything."""
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return True


class ClawTalkAdapter(BasePlatformAdapter):
    """Bridges the ClawTalk event socket into the Hermes gateway."""

    # SMS is the only length-capped channel and it is capped in send() with a
    # hard truncate rather than a split, so no generic chunking here.
    MAX_MESSAGE_LENGTH = 0

    def __init__(self, config: PlatformConfig, **_kwargs: Any) -> None:
        super().__init__(config=config, platform=Platform(PLATFORM_NAME))

        extra = dict(getattr(config, "extra", None) or {})
        # Publish into the process-wide runtime rather than building a private
        # one. Tool handlers reach the same graph through get_runtime(), and
        # they must: clawtalk_approve registers its waiter on the approval
        # manager that this adapter's socket will later resolve, and
        # clawtalk_status reads this adapter's live connection state.
        self._runtime = get_runtime(extra=extra)
        self._config = self._runtime.config

        self._ws: WebSocketService | None = None
        self._observer: MissionObserver | None = None
        self._mission_events = MissionEventHandler(self._runtime.missions)

        # Correlation state. Voice and walkie replies must carry the
        # request_id of the frame that asked for them, but the gateway hands
        # replies back keyed only by chat id, so we hold the mapping here.
        self._pending_deep: dict[str, str] = {}
        self._pending_walkie: dict[str, str] = {}
        # digits -> original E.164, so SMS replies go to a dialable number.
        self._sms_numbers: dict[str, str] = {}
        # call_id -> already greeted
        self._greeted: dict[str, bool] = {}

        self._report_call_outcomes = _as_bool(extra.get("report_call_outcomes"), True)

    # -- authorization -----------------------------------------------------

    @property
    def authorization_is_upstream(self) -> bool:
        """ClawTalk authorizes callers before anything reaches this gateway.

        The socket is authenticated with the account's own API key, and the
        server applies PIN auth, caller whitelisting, paranoid mode, and
        prompt screening *before* it forwards an event. The sender is a phone
        number, not a platform account the operator configures here, so the
        usual ``{PLATFORM}_ALLOWED_USERS`` allowlist has nothing to match on
        and default-denying would drop every legitimate call.

        This is delegation to an authenticated upstream, not a fail-open -
        and an operator who wants a *local* allowlist on top can set
        ``CLAWTALK_ALLOWED_USERS``, which turns this off and hands
        authorization back to the gateway.
        """
        return not self._config.allowed_users

    # -- lifecycle ---------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self._config.has_api_key:
            message = (
                "[clawtalk] no API key configured. Set CLAWTALK_API_KEY or "
                "gateway.platforms.clawtalk.extra.api_key"
            )
            logger.error(message)
            self._set_fatal_error("clawtalk_missing_api_key", message, retryable=False)
            return False

        if not check_clawtalk_requirements():
            message = "[clawtalk] aiohttp is not installed (pip install aiohttp)"
            logger.error(message)
            self._set_fatal_error("clawtalk_missing_deps", message, retryable=False)
            return False

        self._runtime.ws_log.open()

        self._ws = WebSocketService(self._config, ws_log=self._runtime.ws_log)
        self._runtime.ws = self._ws
        self._register_handlers(self._ws)

        if self._config.auto_connect:
            await self._ws.start()
        else:
            logger.info("[clawtalk] auto_connect disabled; socket not started")

        if self._config.missions.enabled:
            self._observer = MissionObserver(
                self._runtime.missions,
                self._dispatch_observer_prompt,
                self._config.missions.observer,
            )
            self._observer.start()

        self._running = True
        logger.info("[clawtalk] connected (server: %s)", self._config.server)
        return True

    async def disconnect(self) -> None:
        if self._observer is not None:
            await self._observer.stop()
            self._observer = None

        if self._ws is not None:
            await self._ws.stop()
            self._ws = None
        self._runtime.ws = None

        self._runtime.approvals.cleanup_pending()
        self._runtime.ws_log.close()
        self._running = False
        logger.info("[clawtalk] disconnected")

    def _register_handlers(self, ws: WebSocketService) -> None:
        ws.on(wire.EVENT_CONTEXT_REQUEST, self._on_context_request)
        ws.on(wire.EVENT_CALL_STARTED, self._on_call_started)
        ws.on(wire.EVENT_CALL_ENDED, self._on_call_ended)
        ws.on(wire.EVENT_DEEP_TOOL_REQUEST, self._on_deep_tool_request)
        ws.on(wire.EVENT_SMS_RECEIVED, self._on_sms_received)
        ws.on(wire.EVENT_WALKIE_REQUEST, self._on_walkie_request)
        ws.on(wire.EVENT_APPROVAL_RESPONDED, self._on_approval_responded)
        for event in wire.MISSION_EVENTS:
            ws.on(event, self._on_mission_event)
        ws.on("request_logs", self._on_request_logs)
        ws.on("disconnected", self._on_ws_disconnected)

    # -- outbound ----------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Route an agent reply to the right ClawTalk channel.

        The chat-id prefix selects the transport; see the module docstring.
        """
        if chat_id.startswith(CHAT_CALL):
            return await self._send_voice(chat_id[len(CHAT_CALL) :], content)
        if chat_id.startswith(CHAT_WALKIE):
            return await self._send_walkie(chat_id[len(CHAT_WALKIE) :], content)
        if chat_id.startswith(CHAT_SMS):
            return await self._send_sms(chat_id[len(CHAT_SMS) :], content)
        if chat_id.startswith(CHAT_MISSION) or chat_id == CHAT_CALL_EVENTS:
            # These sessions have no outbound channel: the agent acts through
            # mission tools, and the text is its reasoning, not a message.
            logger.info("[clawtalk] %s reply (not delivered): %s", chat_id, truncate(content, 200))
            return SendResult(success=True)

        return SendResult(
            success=False,
            error=f"Unknown ClawTalk chat id: {chat_id}",
            error_kind="not_found",
        )

    async def _send_voice(self, call_id: str, content: str) -> SendResult:
        """Answer the outstanding deep-tool request, or speak a follow-up.

        The first reply for a request settles it as ``deep_tool_result``.
        Anything the agent streams afterwards is spoken with a plain
        ``response`` frame, which is what gives the caller drip progress
        instead of dead air.
        """
        if self._ws is None:
            return SendResult(success=False, error="WebSocket not connected", retryable=True)

        text = clean_text_for_voice(content) or "Done."
        request_id = self._pending_deep.pop(call_id, None)

        frame = (
            wire.deep_tool_result(call_id, request_id, text)
            if request_id
            else wire.response(call_id, text)
        )
        ok = await self._ws.try_send(frame)
        return (
            SendResult(success=True, message_id=request_id or call_id)
            if ok
            else SendResult(success=False, error="WebSocket send failed", retryable=True)
        )

    async def _send_walkie(self, session: str, content: str) -> SendResult:
        if self._ws is None:
            return SendResult(success=False, error="WebSocket not connected", retryable=True)

        request_id = self._pending_walkie.pop(session, None)
        if not request_id:
            # A walkie request is a single question with a single answer;
            # extra output has nowhere to go.
            logger.debug("[clawtalk] no pending walkie request for %s; dropping reply", session)
            return SendResult(success=True)

        text = clean_text_for_voice(content) or "Done."
        ok = await self._ws.try_send(wire.walkie_response(request_id, reply=text))
        return (
            SendResult(success=True, message_id=request_id)
            if ok
            else SendResult(success=False, error="WebSocket send failed", retryable=True)
        )

    async def _send_sms(self, digits: str, content: str) -> SendResult:
        to = self._sms_numbers.get(digits) or (f"+{digits}" if digits else "")
        if not to:
            return SendResult(success=False, error="No destination number", error_kind="not_found")

        body = truncate(content.strip(), self._config.sms_max_reply_chars)
        try:
            result = await asyncio.to_thread(
                self._runtime.client.sms.send, to=to, message=body
            )
        except Exception as exc:  # noqa: BLE001 - normalized into SendResult
            logger.error("[clawtalk] SMS send to %s failed: %s", mask_phone(to), exc)
            return SendResult(success=False, error=str(exc), retryable=True)

        logger.info("[clawtalk] SMS reply sent to %s", mask_phone(to))
        return SendResult(success=True, message_id=str(result.get("id") or ""))

    async def send_typing(self, chat_id: str, metadata: dict[str, Any] | None = None) -> None:
        """No-op: neither telephony nor SMS has a typing indicator."""
        return None

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        if chat_id.startswith(CHAT_SMS):
            digits = chat_id[len(CHAT_SMS) :]
            return {
                "chat_id": chat_id,
                "name": self._sms_numbers.get(digits, f"+{digits}"),
                "type": "dm",
            }
        return {"chat_id": chat_id, "name": chat_id, "type": "dm"}

    # -- inbound: voice ----------------------------------------------------

    async def _on_context_request(self, message: dict[str, Any]) -> None:
        """Ship the voice system prompt and greeting at call setup.

        Answered directly from the adapter: this is configuration for the
        Telnyx voice AI, not something the agent needs a turn for, and the
        caller is already on the line waiting.
        """
        call_id = str(message.get("call_id") or "")
        if not call_id or self._ws is None:
            return

        logger.info("[clawtalk] call started (context_request): %s", call_id)
        self._greeted[call_id] = True

        await self._ws.try_send(
            wire.context_response(
                call_id,
                memory=CALL_MEMORY_BLURB,
                system_prompt=self._runtime.voice.build_context(),
            )
        )
        logger.info("[clawtalk] context sent for call: %s", call_id)
        await self._send_greeting(call_id)

    async def _on_call_started(self, message: dict[str, Any]) -> None:
        call_id = str(message.get("call_id") or "")
        direction = message.get("direction")
        if not call_id:
            return

        logger.info("[clawtalk] call started: %s direction=%s", call_id, direction)
        if direction == "inbound" and not self._greeted.get(call_id):
            await self._send_greeting(call_id)
            self._greeted[call_id] = True
        else:
            self._greeted.setdefault(call_id, False)

    async def _on_call_ended(self, message: dict[str, Any]) -> None:
        call_id = str(message.get("call_id") or "")
        self._greeted.pop(call_id, None)
        self._pending_deep.pop(call_id, None)
        logger.info("[clawtalk] call ended: %s", call_id)

        summary = build_outcome_summary(message)
        if not summary:
            return

        logger.info("[clawtalk] %s", summary)
        if self._report_call_outcomes:
            await self._dispatch(
                chat_id=CHAT_CALL_EVENTS,
                text=f"[ClawTalk] {summary}",
                channel_prompt=CALL_EVENTS_PROMPT,
                user_id="clawtalk",
                user_name="ClawTalk",
            )

    async def _send_greeting(self, call_id: str) -> None:
        if self._ws is None:
            return
        if await self._ws.try_send(wire.response(call_id, self._runtime.voice.greeting)):
            logger.info("[clawtalk] greeting sent")

    async def _on_deep_tool_request(self, message: dict[str, Any]) -> None:
        """Route a mid-call tool request into this call's Hermes session."""
        call_id = str(message.get("call_id") or "")
        request_id = str(message.get("request_id") or "")
        query = str(message.get("query") or "")
        if not call_id or not request_id:
            return

        logger.info("[clawtalk] deep tool request [%s]: %s", request_id, truncate(query, 100))
        self._pending_deep[call_id] = request_id

        await self._dispatch(
            chat_id=f"{CHAT_CALL}{call_id}",
            text=VOICE_PREFIX + query,
            channel_prompt=self._runtime.voice.build_context(),
            user_id=f"call:{call_id}",
            user_name="Caller",
            message_id=request_id,
            raw=message,
        )

    async def _on_walkie_request(self, message: dict[str, Any]) -> None:
        request_id = str(message.get("request_id") or "")
        transcript = str(message.get("transcript") or "")
        session = str(message.get("session_key") or DEFAULT_WALKIE_SESSION)
        if not request_id:
            return

        logger.info("[clawtalk] walkie request [%s]: %s", request_id, truncate(transcript, 100))
        self._pending_walkie[session] = request_id

        await self._dispatch(
            chat_id=f"{CHAT_WALKIE}{session}",
            text=transcript,
            channel_prompt=WALKIE_PREFIX + self._runtime.voice.build_context(),
            user_id="walkie",
            user_name="Walkie",
            message_id=request_id,
            raw=message,
        )

    # -- inbound: SMS ------------------------------------------------------

    async def _on_sms_received(self, message: dict[str, Any]) -> None:
        sender = str(message.get("from") or "")
        body = str(message.get("body") or "")
        media_urls = list(message.get("media_urls") or [])
        if not sender:
            return

        logger.info(
            "[clawtalk] SMS received from %s: %s", mask_phone(sender), truncate(body, 50)
        )

        digits = normalize_phone(sender)
        self._sms_numbers[digits] = sender

        text = body
        if media_urls:
            logger.info("[clawtalk] MMS with %d media attachment(s)", len(media_urls))
            listing = "\n".join(f"- {url}" for url in media_urls)
            text = (
                f"{text}\n\n[MMS Attachments - analyze these images using the "
                f"image tool]\n{listing}"
            )

        await self._dispatch(
            chat_id=f"{CHAT_SMS}{digits}",
            text=text,
            channel_prompt=SMS_SYSTEM_PROMPT.format(limit=self._config.sms_max_reply_chars),
            user_id=sender,
            user_name=sender,
            chat_name=sender,
            message_id=str(message.get("message_id") or ""),
            raw=message,
        )

    # -- inbound: missions and control ------------------------------------

    async def _on_mission_event(self, message: dict[str, Any]) -> None:
        prepared = await asyncio.to_thread(self._mission_events.prepare, message)
        if prepared is None:
            return

        logger.info(
            "[clawtalk] mission event %s -> session mission:%s",
            message.get("event"),
            prepared.slug,
        )
        await self._dispatch(
            chat_id=f"{CHAT_MISSION}{prepared.slug}",
            text=prepared.text,
            channel_prompt=prepared.channel_prompt,
            user_id="clawtalk",
            user_name="ClawTalk Missions",
            raw=message,
        )

    async def _dispatch_observer_prompt(
        self, chat_id: str, text: str, channel_prompt: str | None
    ) -> bool:
        await self._dispatch(
            chat_id=chat_id,
            text=text,
            channel_prompt=channel_prompt,
            user_id="clawtalk",
            user_name="ClawTalk Observer",
        )
        return True

    def _on_approval_responded(self, message: dict[str, Any]) -> None:
        self._runtime.approvals.handle_ws_response(message)

    def _on_ws_disconnected(self, code: Any, reason: Any) -> None:
        # A blocked clawtalk_approve call can no longer receive its decision,
        # so release every waiter instead of letting them sit out the timeout.
        self._runtime.approvals.cleanup_pending()

    async def _on_request_logs(self, request_id: str) -> None:
        """Serve a tail of ``ws.log`` back over the socket."""
        if self._ws is None or not request_id:
            return
        try:
            lines = await asyncio.to_thread(
                self._runtime.ws_log.read_recent_lines, WS_LOG_TAIL_LINES
            )
            frame = wire.logs_response(request_id, lines)
        except Exception as exc:  # noqa: BLE001 - always answer the request
            frame = wire.logs_response(request_id, [], error=str(exc))
        await self._ws.try_send(frame)

    # -- dispatch helper ---------------------------------------------------

    async def _dispatch(
        self,
        *,
        chat_id: str,
        text: str,
        channel_prompt: str | None = None,
        user_id: str = "clawtalk",
        user_name: str = "ClawTalk",
        chat_name: str | None = None,
        message_id: str = "",
        raw: Any = None,
    ) -> None:
        """Hand one inbound event to the gateway as a normal agent turn."""
        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_name or chat_id,
            chat_type="dm",
            user_id=user_id,
            user_name=user_name,
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            user_id=user_id,
            user_name=user_name,
            source=source,
            raw_message=raw,
            message_id=message_id,
            channel_prompt=channel_prompt,
            # Callers and texters are external parties. Their words must stay
            # conversational input and never resolve a gateway slash command.
            allow_gateway_control=False,
        )

        task = asyncio.create_task(self.handle_message(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)


def build_outcome_summary(event: dict[str, Any]) -> str | None:
    """Render a human-readable summary of a finished call.

    Ported from the OpenClaw plugin's ``reportCallOutcome`` so both gateways
    describe the same call the same way.
    """
    from .formatting import format_duration, format_phone_number

    direction = event.get("direction") or "unknown"
    duration = event.get("duration_seconds") or 0
    reason = event.get("reason") or "unknown"
    outcome = event.get("outcome")
    to_number = event.get("to_number")
    purpose = event.get("purpose")
    greeting = event.get("greeting")
    voicemail = event.get("voicemail_message")

    if direction == "outbound":
        target = format_phone_number(to_number) if to_number else "unknown number"

        if outcome == "voicemail":
            summary = f"Voicemail left for {target}"
            if voicemail:
                summary += f'\n> "{truncate(voicemail, 200)}"'
            return summary
        if outcome == "voicemail_failed":
            return (
                f"Call to {target} went to voicemail but couldn't leave a "
                f"message (no beep detected)"
            )
        if outcome == "no_answer" or reason == "amd_silence":
            return f"Call to {target} - no answer (silence detected)"
        if outcome == "fax":
            return f"Call to {target} - fax machine detected, call ended"

        if reason == "user_hangup":
            summary = f"Call to {target} completed ({format_duration(duration)})"
        else:
            summary = f"Call to {target} ended: {reason} ({format_duration(duration)})"
        if purpose or greeting:
            summary += f"\nPurpose: {truncate(str(purpose or greeting), 100)}"
        return summary

    if direction == "inbound":
        return f"Inbound call ended ({format_duration(duration)})"

    return f"Call ended: {reason}"


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
