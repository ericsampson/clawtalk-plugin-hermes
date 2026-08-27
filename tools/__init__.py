"""ClawTalk agent tools.

One flat registry so ``register(ctx)`` stays a loop and the tool list has a
single source of truth, mirrored in ``plugin.yaml``'s ``provides_tools``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, NamedTuple

from .approve import APPROVE_SCHEMA, clawtalk_approve
from .assistants import (
    ASSISTANTS_SCHEMA,
    INSIGHTS_SCHEMA,
    clawtalk_assistants,
    clawtalk_insights,
)
from .bot_config import BOT_CONFIG_SCHEMA, clawtalk_bot_config
from .call import CALL_SCHEMA, CALL_STATUS_SCHEMA, clawtalk_call, clawtalk_call_status
from .common import TOOLSET
from .mission import (
    MISSION_CANCEL_EVENT_SCHEMA,
    MISSION_COMPLETE_SCHEMA,
    MISSION_EVENT_STATUS_SCHEMA,
    MISSION_GET_PLAN_SCHEMA,
    MISSION_INIT_SCHEMA,
    MISSION_LIST_SCHEMA,
    MISSION_LOG_EVENT_SCHEMA,
    MISSION_MEMORY_SCHEMA,
    MISSION_SCHEDULE_SCHEMA,
    MISSION_SETUP_AGENT_SCHEMA,
    MISSION_UPDATE_STEP_SCHEMA,
    clawtalk_mission_cancel_event,
    clawtalk_mission_complete,
    clawtalk_mission_event_status,
    clawtalk_mission_get_plan,
    clawtalk_mission_init,
    clawtalk_mission_list,
    clawtalk_mission_log_event,
    clawtalk_mission_memory,
    clawtalk_mission_schedule,
    clawtalk_mission_setup_agent,
    clawtalk_mission_update_step,
)
from .sms import (
    SMS_CONVERSATIONS_SCHEMA,
    SMS_LIST_SCHEMA,
    SMS_SCHEMA,
    clawtalk_sms,
    clawtalk_sms_conversations,
    clawtalk_sms_list,
)
from .status import STATUS_SCHEMA, clawtalk_status

__all__ = ["CORE_TOOLS", "MISSION_TOOLS", "ToolSpec", "register_tools", "tool_names"]

logger = logging.getLogger(__name__)


class ToolSpec(NamedTuple):
    """One registerable tool: JSON schema plus its handler."""

    schema: dict[str, Any]
    handler: Callable[..., str]
    emoji: str = ""

    @property
    def name(self) -> str:
        return str(self.schema["name"])

    @property
    def description(self) -> str:
        return str(self.schema.get("description", ""))


#: Everything except missions. Always registered.
CORE_TOOLS: list[ToolSpec] = [
    ToolSpec(BOT_CONFIG_SCHEMA, clawtalk_bot_config, "🎛️"),
    ToolSpec(CALL_SCHEMA, clawtalk_call, "📞"),
    ToolSpec(CALL_STATUS_SCHEMA, clawtalk_call_status, "📞"),
    ToolSpec(SMS_SCHEMA, clawtalk_sms, "💬"),
    ToolSpec(SMS_LIST_SCHEMA, clawtalk_sms_list, "💬"),
    ToolSpec(SMS_CONVERSATIONS_SCHEMA, clawtalk_sms_conversations, "💬"),
    ToolSpec(APPROVE_SCHEMA, clawtalk_approve, "✅"),
    ToolSpec(STATUS_SCHEMA, clawtalk_status, "📊"),
    ToolSpec(ASSISTANTS_SCHEMA, clawtalk_assistants, "🤖"),
    ToolSpec(INSIGHTS_SCHEMA, clawtalk_insights, "🔍"),
]

#: Registered only when missions are enabled, so a user who does not run
#: campaigns does not carry eleven extra schemas in every prompt.
MISSION_TOOLS: list[ToolSpec] = [
    ToolSpec(MISSION_INIT_SCHEMA, clawtalk_mission_init, "🎯"),
    ToolSpec(MISSION_SETUP_AGENT_SCHEMA, clawtalk_mission_setup_agent, "🎯"),
    ToolSpec(MISSION_SCHEDULE_SCHEMA, clawtalk_mission_schedule, "🗓️"),
    ToolSpec(MISSION_EVENT_STATUS_SCHEMA, clawtalk_mission_event_status, "🗓️"),
    ToolSpec(MISSION_COMPLETE_SCHEMA, clawtalk_mission_complete, "🏁"),
    ToolSpec(MISSION_UPDATE_STEP_SCHEMA, clawtalk_mission_update_step, "🎯"),
    ToolSpec(MISSION_LOG_EVENT_SCHEMA, clawtalk_mission_log_event, "📝"),
    ToolSpec(MISSION_MEMORY_SCHEMA, clawtalk_mission_memory, "🧠"),
    ToolSpec(MISSION_LIST_SCHEMA, clawtalk_mission_list, "🎯"),
    ToolSpec(MISSION_GET_PLAN_SCHEMA, clawtalk_mission_get_plan, "📋"),
    ToolSpec(MISSION_CANCEL_EVENT_SCHEMA, clawtalk_mission_cancel_event, "🚫"),
]


def tool_names(include_missions: bool = True) -> list[str]:
    specs = CORE_TOOLS + (MISSION_TOOLS if include_missions else [])
    return [spec.name for spec in specs]


def register_tools(ctx: Any, include_missions: bool = True) -> int:
    """Register every tool with the plugin context. Returns the count."""
    specs = CORE_TOOLS + (MISSION_TOOLS if include_missions else [])

    for spec in specs:
        ctx.register_tool(
            name=spec.name,
            toolset=TOOLSET,
            schema=spec.schema,
            handler=spec.handler,
            description=spec.description,
            emoji=spec.emoji,
        )

    logger.debug("[clawtalk] registered %d agent tools", len(specs))
    return len(specs)
