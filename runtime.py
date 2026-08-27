"""Lazily-built, process-wide service graph.

The OpenClaw plugin used a module-level ``globalRuntime`` so repeated
``register()`` calls could not open a second WebSocket. The same reasoning
applies here for a different reason: tool handlers, the CLI, and the gateway
adapter all need the same client, mission state, and approval manager, but
they are entered from three different places and only one of them (the
adapter) is guaranteed to exist.

So: build the REST-side graph on first use, and let the adapter *publish*
itself into that graph when the gateway comes up. A tool invoked inside a
running gateway therefore sees live WebSocket state; the same tool invoked
from ``hermes chat`` degrades to REST-only instead of failing.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ClawTalkConfig, load_config
from .sdk import ClawTalkClient
from .services.approvals import ApprovalManager
from .services.doctor import DoctorService
from .services.missions import MissionService
from .services.voice import VoiceService
from .version import __version__
from .ws_logger import WsLogger

__all__ = [
    "ClawTalkRuntime",
    "get_runtime",
    "reset_runtime",
    "set_plugin_context",
]

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_runtime: ClawTalkRuntime | None = None
_ctx: Any = None
_data_dir_override: Path | None = None


@dataclass
class ClawTalkRuntime:
    """Everything the plugin needs that is not tied to the event loop."""

    config: ClawTalkConfig
    client: ClawTalkClient
    missions: MissionService
    approvals: ApprovalManager
    voice: VoiceService
    ws_log: WsLogger
    data_dir: Path
    #: Set by ClawTalkAdapter.connect() while a gateway owns this process.
    ws: Any = None

    @property
    def doctor(self) -> DoctorService:
        """A doctor bound to the current WebSocket state."""
        return DoctorService(
            self.client, ws=self.ws, has_api_key=self.config.has_api_key
        )

    @property
    def ws_connected(self) -> bool:
        return bool(self.ws is not None and self.ws.is_connected)


def set_plugin_context(ctx: Any) -> None:
    """Remember the plugin context so later lazy builds can read its config.

    Called once from ``register(ctx)``. Also resets any cached runtime, since
    a reload may have changed the config underneath us.
    """
    global _ctx
    with _lock:
        _ctx = ctx
        reset_runtime()


def resolve_data_dir() -> Path:
    """Where ``ws.log`` and ``missions_state.json`` live.

    Prefers the plugin's own profile-scoped data directory, falling back to
    ``~/.hermes/plugin-data/clawtalk`` when no context is available (for
    example inside ``hermes clawtalk doctor`` before discovery has run).
    """
    if _data_dir_override is not None:
        return _data_dir_override

    state = getattr(_ctx, "state", None) if _ctx is not None else None
    data_dir = getattr(state, "data_dir", None)
    if data_dir:
        return Path(data_dir)

    hermes_home = os.getenv("HERMES_HOME") or os.path.join(
        os.path.expanduser("~"), ".hermes"
    )
    return Path(hermes_home) / "plugin-data" / "clawtalk"


def build_runtime(
    extra: dict | None = None, data_dir: Path | None = None
) -> ClawTalkRuntime:
    """Construct a fresh service graph. Prefer :func:`get_runtime`."""
    config = load_config(extra=extra, ctx=_ctx)
    directory = data_dir or resolve_data_dir()
    directory.mkdir(parents=True, exist_ok=True)

    client = ClawTalkClient(
        api_key=config.api_key,
        server=config.server,
        client_version=__version__,
    )
    ws_log = WsLogger(directory / "ws.log")

    return ClawTalkRuntime(
        config=config,
        client=client,
        missions=MissionService(client, directory),
        approvals=ApprovalManager(client),
        voice=VoiceService(config),
        ws_log=ws_log,
        data_dir=directory,
    )


def get_runtime(extra: dict | None = None) -> ClawTalkRuntime:
    """Return the process-wide runtime, building it on first use.

    Passing *extra* (the adapter's ``PlatformConfig.extra``) forces a rebuild
    so gateway-scoped config wins over whatever a tool built earlier.
    """
    global _runtime
    with _lock:
        if _runtime is None or extra is not None:
            _runtime = build_runtime(extra=extra)
            logger.debug(
                "[clawtalk] runtime built (server=%s, data_dir=%s)",
                _runtime.config.server,
                _runtime.data_dir,
            )
        return _runtime


def reset_runtime() -> None:
    """Drop the cached runtime, closing anything it owns."""
    global _runtime
    with _lock:
        if _runtime is not None:
            _runtime.ws_log.close()
        _runtime = None
