"""CP-TEST H1/H2 locking: identity nahi speaks; kitni emi → which-EMI."""

from __future__ import annotations

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.retrieval import clear_retrieval_cache
from app.engine.tenant_profile import clear_tenant_profile_cache
from app.engine.turn import handle_turn
from app.flows.loader import reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest

CALL_DATE = "2026-08-15"
TENANT = "paisalo"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("SCRIPTED_CATALOG_ROUTING", "true")
    monkeypatch.setenv("CALL_WINDOW_START", "00:00")
    monkeypatch.setenv("CALL_WINDOW_END", "23:59")
    monkeypatch.setenv("TEST_PLO_SCENARIO", "postdue1")
    monkeypatch.setenv("COMMITMENT_GATE_ENFORCE", "true")
    clear_tenant_profile_cache()
    get_settings.cache_clear()
    reload_flow_set()
    clear_retrieval_cache()
    yield
    clear_retrieval_cache()
    clear_tenant_profile_cache()
    get_settings.cache_clear()


class _EmptyKB:
    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def retrieve(self, text, tenant_id, k: int = 6):
        return []


class _StubLLM:
    async def ping(self) -> bool:
        return True

    @property
    def is_stub(self) -> bool:
        return False

    async def complete(self, system, user, *, json_only=True, **kw) -> str:
        return "[]"


def _req(call_id: str, text: str) -> TurnRequest:
    return TurnRequest(
        call_id=call_id,
        borrower_id="plo_test_borrower",
        tenant_id=TENANT,
        channel="voice",
        locale="hi-IN",
        transcript=text,
        turn_meta={"force_flow": "plo_opener", "call_date": CALL_DATE},
    )


@pytest.mark.asyncio
async def test_h1_identity_nahi_speaks_wrong_number_no_crash():
    memory = InMemoryMemoryStore()
    llm = _StubLLM()
    t0 = await handle_turn(
        _req("h1-id", ""), memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB()
    )
    t1 = await handle_turn(
        _req("h1-id", "nahi"),
        memory=memory,
        llm=llm,
        tools=FakeToolClient(),
        kb=_EmptyKB(),
    )
    assert t0.reply_text
    assert t1.reply_id == "plo_wrong_number"
    assert (t1.reply_text or "").strip()
    assert t1.end_call is True


@pytest.mark.asyncio
async def test_h2_kitni_emi_hai_routes_which_emi_pd():
    memory = InMemoryMemoryStore()
    llm = _StubLLM()
    await handle_turn(
        _req("h2-emi", ""), memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB()
    )
    await handle_turn(
        _req("h2-emi", "haan, main Ramesh bol raha hoon"),
        memory=memory,
        llm=llm,
        tools=FakeToolClient(),
        kb=_EmptyKB(),
    )
    t3 = await handle_turn(
        _req("h2-emi", "kitni emi hai"),
        memory=memory,
        llm=llm,
        tools=FakeToolClient(),
        kb=_EmptyKB(),
    )
    assert t3.reply_id == "plo_obj_which_emi_pd"
    assert (t3.reply_text or "").strip()
