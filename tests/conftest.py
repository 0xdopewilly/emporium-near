"""Test harness: loads the plugin directory as the package ``emporium`` inside a Hermes checkout's
Python (``gateway``, ``plugins`` and ``hermes_constants`` must be importable), with HERMES_HOME in a
temp dir so the ledger never touches a real profile.

    HERMES=/path/to/hermes-agent/.venv/bin/python
    $HERMES -m pytest tests
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

if "emporium" not in sys.modules:
    spec = importlib.util.spec_from_file_location("emporium", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["emporium"] = mod
    spec.loader.exec_module(mod)

# Throwaway keys used only by the tests (no funds, never on any network).
BUYER_KEY = "0x" + "11" * 32
SHOP_ADDR = "0x" + "ab" * 20


SEALED_CONFIG = "memory:\n  memory_enabled: false\n  user_profile_enabled: false\n"


@pytest.fixture(autouse=True)
def hermes_home(tmp_path, monkeypatch):
    """A sealed shop profile (built-in memory off), in a temp dir; the working directory has no context files."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "config.yaml").write_text(SEALED_CONFIG)
    (tmp_path / "work").mkdir()
    monkeypatch.chdir(tmp_path / "work")
    return tmp_path / "home"


@pytest.fixture
def facilitator():
    from fake_facilitator import FakeFacilitator
    f = FakeFacilitator().start()
    yield f
    f.stop()


def settings_for(fac_url: str, **over):
    base = {
        "pay_to": SHOP_ADDR, "network": "eip155:84532", "shop_name": "Test Emporium", "facilitator_url": fac_url,
        "host": "127.0.0.1", "port": 0,
        "services": [
            {"id": "echo", "title": "Echo", "price": "$0.05", "instructions": "Repeat the input.", "tools": []},
            {"id": "pricey", "title": "Pricey", "price": "$3.00", "instructions": "x", "tools": []},
            {"id": "search", "title": "Search", "price": "$0.10", "instructions": "x", "tools": ["web_search"]},
        ],
    }
    base.update(over)
    return base


class AgentStub:
    """Stands in for the gateway: answers each job by calling adapter.send(..., notify=True)."""

    def __init__(self, adapter, reply=None, fail=False, silent=False, turn_failed=False, no_model=False, delay=0.0):
        self.adapter, self.fail, self.silent, self.delay = adapter, fail, silent, delay
        self.reply = reply or (lambda text: "ECHO: " + re.split(r"<<<BUYER INPUT [0-9a-f]+\n", text, maxsplit=1)[-1]
                               .rsplit("\nBUYER INPUT ", 1)[0])
        self.turn_failed, self.no_model = turn_failed, no_model
        self.events = []

    async def __call__(self, event):
        self.events.append(event)
        from gateway.platforms.event import ProcessingOutcome
        from emporium import guard
        if self.silent:
            return
        if self.delay:
            await asyncio.sleep(self.delay)
        # What Hermes's hooks report during a real turn: the turn starts (pre_llm_call), the model
        # answers (post_api_request), and the turn ends (on_session_end).
        sid = "sess-" + event.message_id
        guard.SESSIONS.on_llm_call(sid, "emporium", event.text, task_id=sid)
        if not self.no_model:
            guard.SESSIONS.mark_llm_ok(sid, sid)
        if self.turn_failed:
            guard.SESSIONS.mark_turn_end(sid, sid, completed=False, failed=True, interrupted=False, reason="api_error")
            await self.adapter.send(event.source.chat_id, "Sorry, I encountered an error. Please try again.",
                                    metadata={"notify": True})
            return
        if self.fail:
            await self.adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)
            return
        await self.adapter.send(event.source.chat_id, self.reply(event.text), metadata={"notify": True})


def start_shop(settings: dict):
    """A running EmporiumAdapter on a random port for these settings, with the agent stubbed out.
    Returns (adapter, stop)."""
    from gateway.config import PlatformConfig
    from gateway.platform_registry import PlatformEntry, platform_registry
    from emporium.adapter import EmporiumAdapter

    if "emporium" not in platform_registry.registered_names():
        platform_registry.register(PlatformEntry(name="emporium", label="Emporium", adapter_factory=lambda c: None,
                                                 check_fn=lambda: True))

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    adapter = EmporiumAdapter(PlatformConfig(enabled=True), lambda k, d=None: settings.get(k, d), "test")
    adapter._message_handler = object()  # the gateway's handler is replaced by the stub below
    stub = AgentStub(adapter)
    adapter.handle_message = stub
    ok = asyncio.run_coroutine_threadsafe(adapter.connect(), loop).result(30)
    assert ok, adapter.shop.problems
    port = adapter._httpd.server_address[1]
    adapter.shop.port = port
    adapter.test_url = f"http://127.0.0.1:{port}"
    adapter.stub = stub
    adapter.settings = settings

    def stop():
        asyncio.run_coroutine_threadsafe(adapter.disconnect(), loop).result(10)
        loop.call_soon_threadsafe(loop.stop)
    return adapter, stop


@pytest.fixture
def shop(facilitator):
    """A running EmporiumAdapter on a random port, with the agent stubbed out."""
    adapter, stop = start_shop(settings_for(facilitator.url))
    adapter.facilitator = facilitator
    yield adapter
    stop()


def as_settings(d: dict):
    return lambda k, default=None: d.get(k, default)


def dump(obj) -> str:
    return json.dumps(obj, indent=1, default=str)


@pytest.fixture
def no_backoff(monkeypatch):
    from emporium import adapter
    monkeypatch.setattr(adapter.time, "sleep", lambda s: None)
    return adapter


@pytest.fixture(autouse=True)
def offline_chain(monkeypatch):
    """Tests never reach a real RPC: on-chain checks fail closed unless a test stubs them."""
    from emporium import chain

    def _no_rpc(*a, **k):
        raise RuntimeError("no network in tests")
    monkeypatch.setattr(chain, "_rpc", _no_rpc)
