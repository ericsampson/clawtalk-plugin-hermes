"""Resource namespaces for :class:`~clawtalk.sdk.client.ClawTalkClient`.

Each namespace owns one server resource and knows how that resource's
responses are wrapped. The server is inconsistent about envelopes - some
routes return ``{"assistant": {...}}``, some ``{"data": [...]}``, some the
bare record - so every method unwraps defensively and returns the inner
object. That behaviour is deliberate and matches the OpenClaw SDK.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlencode

from .endpoints import ENDPOINTS, resolve

__all__ = [
    "ApprovalsNamespace",
    "AssistantsNamespace",
    "CallsNamespace",
    "DoctorNamespace",
    "InsightsNamespace",
    "MissionsNamespace",
    "NumbersNamespace",
    "SmsNamespace",
    "UserNamespace",
    "VoicesNamespace",
]

#: ``(method, endpoint, body) -> decoded JSON``
RequestFn = Callable[..., Any]


def _unwrap(result: Any, *keys: str) -> Any:
    """Return the first present envelope key, else the result itself."""
    if isinstance(result, Mapping):
        for key in keys:
            value = result.get(key)
            if value is not None:
                return value
    return result


def _query(params: Mapping[str, Any]) -> str:
    """Build a ``?a=1&b=2`` suffix, dropping empty values."""
    pairs = [(k, str(v)) for k, v in params.items() if v not in (None, "")]
    return f"?{urlencode(pairs)}" if pairs else ""


class _Namespace:
    def __init__(self, request: RequestFn) -> None:
        self._request = request


# -- calls -----------------------------------------------------------------


class CallsNamespace(_Namespace):
    """Outbound call initiation and live-call control."""

    def initiate(
        self,
        to: str | None = None,
        greeting: str | None = None,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        body = {k: v for k, v in {"to": to, "greeting": greeting, "purpose": purpose}.items() if v}
        return self._request("POST", ENDPOINTS["initiateCall"].path, body)

    def status(self, call_id: str) -> dict[str, Any]:
        return self._request(
            "GET", resolve(ENDPOINTS["getCallStatus"].path, {"callId": call_id})
        )

    def end(self, call_id: str, reason: str | None = None) -> dict[str, Any]:
        return self._request(
            "POST",
            resolve(ENDPOINTS["endCall"].path, {"callId": call_id}),
            {"reason": reason} if reason else None,
        )


# -- sms -------------------------------------------------------------------


class SmsNamespace(_Namespace):
    """SMS/MMS send and history."""

    def send(
        self,
        to: str,
        message: str,
        media_urls: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"to": to, "message": message}
        if media_urls:
            body["media_urls"] = list(media_urls)
        return self._request("POST", ENDPOINTS["sendSms"].path, body)

    def list(
        self,
        limit: int | None = None,
        contact: str | None = None,
        direction: str | None = None,
    ) -> dict[str, Any]:
        suffix = _query({"limit": limit, "contact": contact, "direction": direction})
        return self._request("GET", f"{ENDPOINTS['listMessages'].path}{suffix}")

    def conversations(self) -> dict[str, Any]:
        return self._request("GET", ENDPOINTS["listConversations"].path)


# -- approvals -------------------------------------------------------------


class ApprovalsNamespace(_Namespace):
    """Push-notification approval requests."""

    def create(
        self,
        action: str,
        details: str | None = None,
        require_biometric: bool = False,
        expires_in: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "action": action,
            "require_biometric": bool(require_biometric),
        }
        if details:
            body["details"] = details
        if expires_in is not None:
            body["expires_in"] = expires_in
        return self._request("POST", ENDPOINTS["createApproval"].path, body)

    def status(self, request_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            resolve(ENDPOINTS["getApprovalStatus"].path, {"requestId": request_id}),
        )


# -- user / voices ---------------------------------------------------------


class UserNamespace(_Namespace):
    """The authenticated account and its bot configuration."""

    def me(self) -> dict[str, Any]:
        return self._request("GET", ENDPOINTS["getMe"].path)

    def update_me(self, fields: Mapping[str, Any]) -> Any:
        return self._request("PATCH", ENDPOINTS["updateMe"].path, dict(fields))


class VoicesNamespace(_Namespace):
    """The TTS voice catalogue."""

    def list(self, provider: str | None = None) -> dict[str, Any]:
        suffix = _query({"provider": provider})
        return self._request("GET", f"{ENDPOINTS['listVoices'].path}{suffix}")


# -- numbers ---------------------------------------------------------------


class NumbersNamespace(_Namespace):
    """Phone numbers owned by the account."""

    def available(self) -> dict[str, Any]:
        result = self._request("GET", ENDPOINTS["getAvailablePhone"].path)
        return _unwrap(result, "phone")

    def assign(self, phone_id: str, connection_id: str, type_: str | None = None) -> Any:
        body: dict[str, Any] = {"connection_id": connection_id}
        if type_:
            body["type"] = type_
        return self._request(
            "PATCH",
            resolve(ENDPOINTS["assignPhoneNumber"].path, {"phoneId": phone_id}),
            body,
        )


# -- insights / doctor -----------------------------------------------------


class InsightsNamespace(_Namespace):
    """Telnyx conversation insights, proxied by ClawTalk."""

    def get(self, conversation_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            resolve(ENDPOINTS["getInsights"].path, {"conversationId": conversation_id}),
        )


class DoctorNamespace(_Namespace):
    """Server-side health checks, grouped by severity."""

    def critical(self) -> dict[str, Any]:
        return self._request("GET", ENDPOINTS["doctorCritical"].path)

    def warnings(self) -> dict[str, Any]:
        return self._request("GET", ENDPOINTS["doctorWarnings"].path)

    def recommended(self) -> dict[str, Any]:
        return self._request("GET", ENDPOINTS["doctorRecommended"].path)

    def infra(self) -> dict[str, Any]:
        return self._request("GET", ENDPOINTS["doctorInfra"].path)


# -- assistants ------------------------------------------------------------


class ScheduledEventsNamespace(_Namespace):
    """Calls and texts scheduled against an assistant."""

    def schedule(
        self,
        assistant_id: str,
        to: str,
        from_: str,
        scheduled_at: str,
        *,
        text_body: str | None = None,
        mission_id: str | None = None,
        run_id: str | None = None,
        step_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Schedule one event. Presence of *text_body* selects the SMS channel."""
        body: dict[str, Any] = {
            "to": to,
            "from": from_,
            "scheduled_at": scheduled_at,
            "mission_id": mission_id,
            "run_id": run_id,
        }
        if step_id:
            body["step_id"] = step_id
        if metadata:
            body["metadata"] = dict(metadata)

        if text_body is not None:
            body["channel"] = "sms"
            body["text_body"] = text_body
        else:
            body["channel"] = "call"

        result = self._request(
            "POST",
            resolve(ENDPOINTS["scheduleEvent"].path, {"assistantId": assistant_id}),
            body,
        )
        return _unwrap(result, "event")

    def get(self, assistant_id: str, event_id: str) -> dict[str, Any]:
        result = self._request(
            "GET",
            resolve(
                ENDPOINTS["getScheduledEvent"].path,
                {"assistantId": assistant_id, "eventId": event_id},
            ),
        )
        return _unwrap(result, "event")

    def cancel(self, assistant_id: str, event_id: str) -> Any:
        return self._request(
            "DELETE",
            resolve(
                ENDPOINTS["cancelScheduledEvent"].path,
                {"assistantId": assistant_id, "eventId": event_id},
            ),
        )


class AssistantsNamespace(_Namespace):
    """Telnyx voice assistants owned by the account."""

    def __init__(self, request: RequestFn) -> None:
        super().__init__(request)
        self.events = ScheduledEventsNamespace(request)

    def create(
        self,
        name: str,
        instructions: str,
        *,
        greeting: str | None = None,
        voice: str | None = None,
        model: str | None = None,
        description: str | None = None,
        enabled_features: Sequence[str] | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
        extra_config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name, "instructions": instructions}
        if greeting is not None:
            body["greeting"] = greeting
        if voice:
            body["voice"] = voice
        if model:
            body["model"] = model
        if description:
            body["description"] = description
        if enabled_features:
            body["enabled_features"] = list(enabled_features)
        if tools:
            body["tools"] = [dict(tool) for tool in tools]
        if extra_config:
            body["extra_config"] = dict(extra_config)

        result = self._request("POST", ENDPOINTS["createAssistant"].path, body)
        return _unwrap(result, "assistant")

    def get(self, assistant_id: str) -> dict[str, Any]:
        result = self._request(
            "GET", resolve(ENDPOINTS["getAssistant"].path, {"assistantId": assistant_id})
        )
        return _unwrap(result, "assistant")

    def update(self, assistant_id: str, updates: Mapping[str, Any]) -> dict[str, Any]:
        result = self._request(
            "PATCH",
            resolve(ENDPOINTS["updateAssistant"].path, {"assistantId": assistant_id}),
            dict(updates),
        )
        return _unwrap(result, "assistant")

    def list(self, name: str | None = None) -> list[dict[str, Any]]:
        suffix = _query({"name": name})
        result = self._request("GET", f"{ENDPOINTS['listAssistants'].path}{suffix}")
        assistants = _unwrap(result, "assistants")
        return list(assistants) if isinstance(assistants, list) else []

    def connection_id(self, assistant_id: str, feature: str = "telephony") -> str:
        result = self._request(
            "GET",
            resolve(ENDPOINTS["getConnectionId"].path, {"assistantId": assistant_id})
            + _query({"feature": feature}),
        )
        if isinstance(result, Mapping):
            return str(result.get("connection_id", ""))
        return ""

    def assign_phone(
        self, assistant_id: str, phone_number_id: str, connection_id: str,
        type_: str | None = None,
    ) -> Any:
        body: dict[str, Any] = {
            "phone_number_id": phone_number_id,
            "connection_id": connection_id,
        }
        if type_:
            body["type"] = type_
        return self._request(
            "POST",
            resolve(ENDPOINTS["assignPhone"].path, {"assistantId": assistant_id}),
            body,
        )


# -- missions --------------------------------------------------------------


class RunsNamespace(_Namespace):
    """Runs are individual executions of a mission."""

    def create(self, mission_id: str, run_input: Mapping[str, Any]) -> dict[str, Any]:
        result = self._request(
            "POST",
            resolve(ENDPOINTS["createRun"].path, {"missionId": mission_id}),
            {"input": dict(run_input)},
        )
        return _unwrap(result, "data")

    def get(self, mission_id: str, run_id: str) -> dict[str, Any]:
        result = self._request(
            "GET",
            resolve(ENDPOINTS["getRun"].path, {"missionId": mission_id, "runId": run_id}),
        )
        return _unwrap(result, "data")

    def update(self, mission_id: str, run_id: str, updates: Mapping[str, Any]) -> Any:
        return self._request(
            "PATCH",
            resolve(ENDPOINTS["updateRun"].path, {"missionId": mission_id, "runId": run_id}),
            {k: v for k, v in updates.items() if v is not None},
        )

    def list(self, mission_id: str) -> list[dict[str, Any]]:
        result = self._request(
            "GET", resolve(ENDPOINTS["listRuns"].path, {"missionId": mission_id})
        )
        data = _unwrap(result, "data")
        return list(data) if isinstance(data, list) else []


class PlansNamespace(_Namespace):
    """The ordered step list attached to a run."""

    def create(
        self, mission_id: str, run_id: str, steps: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        result = self._request(
            "POST",
            resolve(ENDPOINTS["createPlan"].path, {"missionId": mission_id, "runId": run_id}),
            {"steps": [dict(step) for step in steps]},
        )
        data = _unwrap(result, "data")
        return list(data) if isinstance(data, list) else []

    def get(self, mission_id: str, run_id: str) -> list[dict[str, Any]]:
        result = self._request(
            "GET",
            resolve(ENDPOINTS["getPlan"].path, {"missionId": mission_id, "runId": run_id}),
        )
        data = _unwrap(result, "data")
        return list(data) if isinstance(data, list) else []

    def update_step(
        self, mission_id: str, run_id: str, step_id: str, status: str
    ) -> dict[str, Any]:
        result = self._request(
            "PATCH",
            resolve(
                ENDPOINTS["updateStep"].path,
                {"missionId": mission_id, "runId": run_id, "stepId": step_id},
            ),
            {"status": status},
        )
        return _unwrap(result, "data")


class MissionEventsNamespace(_Namespace):
    """Append-only mission event log."""

    def log(
        self, mission_id: str, run_id: str, event: Mapping[str, Any]
    ) -> dict[str, Any]:
        result = self._request(
            "POST",
            resolve(ENDPOINTS["logEvent"].path, {"missionId": mission_id, "runId": run_id}),
            {k: v for k, v in event.items() if v is not None},
        )
        return _unwrap(result, "data")

    def list(self, mission_id: str, run_id: str) -> list[dict[str, Any]]:
        result = self._request(
            "GET",
            resolve(ENDPOINTS["listEvents"].path, {"missionId": mission_id, "runId": run_id}),
        )
        data = _unwrap(result, "data")
        return list(data) if isinstance(data, list) else []

    def aggregate(self, mission_id: str) -> dict[str, Any]:
        """The portal's combined view: Telnyx events + scheduled + local."""
        return self._request(
            "GET", resolve(ENDPOINTS["getMissionEvents"].path, {"missionId": mission_id})
        )


class MissionAgentsNamespace(_Namespace):
    """Assistants linked to a mission run."""

    def link(self, mission_id: str, run_id: str, agent_id: str) -> Any:
        return self._request(
            "POST",
            resolve(ENDPOINTS["linkAgent"].path, {"missionId": mission_id, "runId": run_id}),
            {"telnyx_agent_id": agent_id},
        )

    def unlink(self, mission_id: str, run_id: str, agent_id: str) -> Any:
        return self._request(
            "DELETE",
            resolve(
                ENDPOINTS["unlinkAgent"].path,
                {"missionId": mission_id, "runId": run_id, "agentId": agent_id},
            ),
        )

    def list(self, mission_id: str, run_id: str) -> list[dict[str, Any]]:
        result = self._request(
            "GET",
            resolve(
                ENDPOINTS["listLinkedAgents"].path,
                {"missionId": mission_id, "runId": run_id},
            ),
        )
        data = _unwrap(result, "data")
        return list(data) if isinstance(data, list) else []


class MissionsNamespace(_Namespace):
    """Missions plus their runs, plans, events, and linked agents."""

    def __init__(self, request: RequestFn) -> None:
        super().__init__(request)
        self.runs = RunsNamespace(request)
        self.plans = PlansNamespace(request)
        self.events = MissionEventsNamespace(request)
        self.agents = MissionAgentsNamespace(request)

    def create(
        self,
        name: str,
        instructions: str,
        *,
        channel: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name, "instructions": instructions}
        if channel:
            body["channel"] = channel
        if metadata:
            body["metadata"] = dict(metadata)
        result = self._request("POST", ENDPOINTS["createMission"].path, body)
        return _unwrap(result, "mission", "data")

    def get(self, mission_id: str) -> dict[str, Any]:
        result = self._request(
            "GET", resolve(ENDPOINTS["getMission"].path, {"missionId": mission_id})
        )
        return _unwrap(result, "mission")

    def list(self, page_size: int = 20) -> list[dict[str, Any]]:
        result = self._request(
            "GET", f"{ENDPOINTS['listMissions'].path}?page[size]={int(page_size)}"
        )
        missions = _unwrap(result, "missions")
        return list(missions) if isinstance(missions, list) else []

    def cancel(self, mission_id: str) -> Any:
        return self._request(
            "POST", resolve(ENDPOINTS["cancelMission"].path, {"missionId": mission_id})
        )
