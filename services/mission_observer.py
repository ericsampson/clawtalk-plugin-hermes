"""Background mission observer.

A pull-based safety net for the push-based
:class:`~clawtalk.services.mission_events.MissionEventHandler`. It ticks on a
fixed interval regardless of chat activity, and for every mission the server
reports as ``running`` it hands the agent the full mission record plus its
event log and lets the agent decide what to do.

This is what unsticks a mission whose ``mission.*`` event was missed during a
WebSocket reconnect gap.

Deviation from the OpenClaw plugin: the original addressed the observer's
session by mission ID while the event handler used the slug, so the two
halves of the same mission talked to two different sessions. Here both
resolve to the slug when one is known, falling back to the mission ID only
for missions with no local state.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import ObserverConfig
from .missions import MissionService

__all__ = ["MissionObserver"]

logger = logging.getLogger(__name__)

MISSION_PAGE_SIZE = 50

#: ``(chat_id, prompt, channel_prompt) -> delivered``
DispatchFn = Callable[[str, str, str | None], Awaitable[bool]]


class MissionObserver:
    """Periodically nudges the agent about every running mission."""

    def __init__(
        self,
        missions: MissionService,
        dispatch: DispatchFn,
        config: ObserverConfig,
    ) -> None:
        self._missions = missions
        self._dispatch = dispatch
        self._config = config
        self._task: asyncio.Task | None = None
        self._last_prompt_at: dict[str, float] = {}

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Begin ticking. A no-op when disabled or already running."""
        if not self._config.enabled:
            logger.info("[clawtalk] mission observer disabled via config")
            return
        if self._task is not None and not self._task.done():
            return

        self._task = asyncio.create_task(self._run(), name="clawtalk:mission-observer")
        logger.info(
            "[clawtalk] mission observer started (%ds interval)",
            round(self._config.interval_s),
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("[clawtalk] mission observer stopped")

    # -- loop --------------------------------------------------------------

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                logger.warning("[clawtalk] mission observer tick failed: %s", exc)
            await asyncio.sleep(self._config.interval_s)

    async def tick(self) -> None:
        """Run one observation pass over every running mission."""
        missions = await asyncio.to_thread(self._fetch_running_missions)
        if not missions:
            logger.debug("[clawtalk] no running missions found")
            return

        logger.info("[clawtalk] running missions: %d", len(missions))

        for mission in missions:
            mission_id = str(mission.get("id") or "")
            if not mission_id:
                continue

            now = time.monotonic()
            last = self._last_prompt_at.get(mission_id)
            if last is not None and now - last < self._config.cooldown_s:
                remaining = round(self._config.cooldown_s - (now - last))
                logger.info(
                    "[clawtalk] skip mission %s (%s) - cooldown (%ds remaining)",
                    mission.get("name"),
                    mission_id,
                    remaining,
                )
                continue

            try:
                delivered = await self._observe(mission_id, str(mission.get("name") or ""))
            except Exception as exc:  # noqa: BLE001 - one bad mission must not
                logger.warning(                      # stall the others
                    "[clawtalk] failed to process mission %s: %s", mission_id, exc
                )
                continue

            if delivered:
                self._last_prompt_at[mission_id] = now

    # -- internals ---------------------------------------------------------

    def _fetch_running_missions(self) -> list[dict[str, Any]]:
        try:
            missions = self._missions.client.missions.list(MISSION_PAGE_SIZE)
        except Exception as exc:  # noqa: BLE001 - server hiccup, retry next tick
            logger.warning("[clawtalk] failed to fetch missions: %s", exc)
            return []
        return [m for m in missions if m.get("status") == "running"]

    async def _observe(self, mission_id: str, name: str) -> bool:
        detail, events, slug = await asyncio.to_thread(self._fetch_context, mission_id)
        chat_id = f"mission:{slug or mission_id}"
        prompt = self._build_prompt(mission_id, name, detail, events)

        logger.info("[clawtalk] sending observer prompt to %s", chat_id)
        return await self._dispatch(chat_id, prompt, None)

    def _fetch_context(self, mission_id: str):
        client = self._missions.client
        detail = client.missions.get(mission_id)
        events = client.missions.events.aggregate(mission_id)
        slug = self._missions.resolve_slug(mission_id)
        return detail, events, slug

    @staticmethod
    def _build_prompt(
        mission_id: str, name: str, detail: Any, events: Any
    ) -> str:
        """Hand the agent the raw records and let it decide what to do.

        Deliberately not pre-digested: the agent has the mission tools and a
        fuller picture of intent than any heuristic here could encode.
        """
        return "\n".join(
            [
                f'[ClawTalk Mission Observer] Mission "{name}" ({mission_id}) is running.',
                "",
                "Mission data:",
                _pretty(detail),
                "",
                "Events data:",
                _pretty(events),
                "",
                "Review the mission status and take any action needed:",
                "- If a call was completed with a non-success status (busy, no-answer, "
                "etc), advance the plan and schedule the next step",
                "- If all plan steps are terminal, complete or fail the mission appropriately",
                "- If the mission appears stuck or stale with no pending events, "
                "consider failing it",
                "- Use `clawtalk_mission_memory` to retrieve any memories saved "
                "during the mission",
                "- If everything is proceeding normally, no action is needed",
            ]
        )


def _pretty(value: Any) -> str:
    try:
        return json.dumps(value, indent=2)
    except (TypeError, ValueError):
        return str(value)
