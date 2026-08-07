"""P0.1 — handle_turn must not re-parse YAML via load_all_flows when cache is warm."""

import json

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.retrieval import clear_retrieval_cache
from app.engine.turn import handle_turn
from app.flows import loader as flow_loader
from app.flows.loader import get_flow_set, reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest


class _EmptyKB:
    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def retrieve(self, text, tenant_id, k: int = 6):
        return []


class _StubLLM:
    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def complete(self, system: str, user: str, *, json_only: bool = True, **kw) -> str:
        return json.dumps(
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}]
        )


@pytest.mark.asyncio
async def test_handle_turn_does_not_call_load_all_flows_when_cache_warm(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    get_settings.cache_clear()
    reload_flow_set()
    clear_retrieval_cache()
    # Warm the cache, then count any further load_all_flows calls.
    assert get_flow_set().flows
    calls = {"n": 0}
    real_load = flow_loader.load_all_flows

    def counting_load(*args, **kwargs):
        calls["n"] += 1
        return real_load(*args, **kwargs)

    monkeypatch.setattr(flow_loader, "load_all_flows", counting_load)
    # Also patch the import sites that bind the name at module load.
    monkeypatch.setattr("app.flows.loader.load_all_flows", counting_load)
    monkeypatch.setattr("app.engine.retrieval.load_all_flows", counting_load)

    memory = InMemoryMemoryStore()
    await handle_turn(
        TurnRequest(
            call_id="flowset-cache-1",
            tenant_id="salary_on_time",
            borrower_id="sot_test_borrower",
            transcript="haan main hi",
            turn_meta={"force_flow": "sot_opener", "call_date": "2026-06-25"},
        ),
        memory=memory,
        kb=_EmptyKB(),
        llm=_StubLLM(),
        tools=FakeToolClient(),
    )
    assert calls["n"] == 0, f"load_all_flows called {calls['n']} times during handle_turn"
