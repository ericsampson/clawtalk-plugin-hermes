"""Test fixtures.

The plugin is a package named ``clawtalk`` regardless of where the repo is
checked out, because Hermes imports it by directory name from
``~/.hermes/plugins/clawtalk``. These fixtures make that true for the tests
too, by exposing the repo root under that name on ``sys.path``.
"""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _unshadow_hermes_packages() -> None:
    """Keep the repo root off ``sys.path``.

    The plugin has its own ``tools/`` package. With the repo root importable,
    a plain ``import tools`` from inside Hermes resolves to ours instead of
    Hermes's, and ``gateway`` fails to import. In a real install the plugin
    lives at ``~/.hermes/plugins/clawtalk`` and is only ever reached as
    ``clawtalk.tools``, so this hazard exists in the test harness alone.
    """
    root = str(REPO_ROOT)
    for entry in ("", ".", root):
        while entry in sys.path:
            sys.path.remove(entry)


def _install_package_alias() -> None:
    """Make ``import clawtalk`` resolve to this repo."""
    _unshadow_hermes_packages()
    if "clawtalk" in sys.modules:
        return
    parent = str(REPO_ROOT.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)

    spec = importlib.util.spec_from_file_location(
        "clawtalk",
        REPO_ROOT / "__init__.py",
        submodule_search_locations=[str(REPO_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["clawtalk"] = module
    spec.loader.exec_module(module)


_install_package_alias()


class FakeResponse:
    """Minimal stand-in for the object ``urlopen`` returns."""

    def __init__(self, status: int, body: str) -> None:
        self._status = status
        self._body = body

    def getcode(self) -> int:
        return self._status

    def read(self) -> bytes:
        return self._body.encode("utf-8")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False


class FakeOpener:
    """Records requests and replays canned responses, keyed by path prefix."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        #: ``"METHOD /path"`` -> body (dict/str) or an Exception to raise.
        self.routes: dict[str, Any] = dict(routes or {})
        self.calls: list[tuple[str, str, Any]] = []
        self.default: Any = {}

    def route(self, key: str, body: Any) -> FakeOpener:
        self.routes[key] = body
        return self

    def open(self, request: Any, timeout: float = 0) -> FakeResponse:
        method = request.get_method()
        path = request.full_url.split("://", 1)[-1].split("/", 1)[-1]
        path = "/" + path
        payload = json.loads(request.data.decode()) if request.data else None
        self.calls.append((method, path, payload))

        key = f"{method} {path}"
        body = self.routes.get(key)
        if body is None:
            # Fall back to a prefix match so query strings do not need routing.
            for route_key, route_body in self.routes.items():
                if key.startswith(route_key):
                    body = route_body
                    break
        if body is None:
            body = self.default
        if isinstance(body, Exception):
            raise body
        return FakeResponse(200, body if isinstance(body, str) else json.dumps(body))


@pytest.fixture
def opener() -> FakeOpener:
    return FakeOpener()


@pytest.fixture
def client(opener: FakeOpener):
    from clawtalk.sdk import ClawTalkClient

    return ClawTalkClient(
        api_key="ct_test_key",
        server="https://clawdtalk.com",
        client_version="0.1.0",
        opener=opener,
    )


@pytest.fixture
def data_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def config():
    from clawtalk.config import ClawTalkConfig

    return ClawTalkConfig(api_key="ct_test_key", server="https://clawdtalk.com")
