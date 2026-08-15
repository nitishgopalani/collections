"""W4-3 TOOLS_LIVE — live degrade, simulate+asterisk refuse, stub seed, rehydrate."""

from __future__ import annotations

import json

import httpx
import pytest

from app.clients.tools_live import LiveToolClient
from app.clients.tools_stub import StubToolClient
from app.config import Settings, get_settings
from app.engine.fragment_library import get_fragment
from app.engine.turn import handle_turn
from app.flows.loader import reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.schemas.state import BorrowerRecord
from app.startup_validation import (
    LiveConfigError,
    collect_live_config_errors,
    validate_live_configuration,
)
from app.engine.retrieval import clear_retrieval_cache
from app.engine.tenant_profile import clear_tenant_profile_cache

CALL_DATE = "2026-08-15"
TENANT = "paisalo"
BORROWER = "plo_test_borrower"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("SCRIPTED_CATALOG_ROUTING", "true")
    monkeypatch.setenv("CALL_WINDOW_START", "00:00")
    monkeypatch.setenv("CALL_WINDOW_END", "23:59")
    monkeypatch.setenv("TEST_PLO_SCENARIO", "postdue3")
    monkeypatch.setenv("COMMITMENT_GATE_ENFORCE", "true")
    monkeypatch.setenv("CARRIER", "")
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


class _ScriptedLLM:
    def __init__(self, turns=None):
        self._responses = [json.dumps(t, ensure_ascii=False) for t in (turns or [])]
        self.call_count = 0

    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def complete(self, system: str, user: str, *, json_only: bool = True, **kw) -> str:
        self.call_count += 1
        if self.call_count <= len(self._responses):
            return self._responses[self.call_count - 1]
        return "[]"


class _TimeoutTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        raise httpx.ReadTimeout("w43 timeout")


def _req(call_id: str, text: str) -> TurnRequest:
    return TurnRequest(
        call_id=call_id,
        borrower_id=BORROWER,
        tenant_id=TENANT,
        channel="voice",
        locale="hi-IN",
        transcript=text,
        turn_meta={"force_flow": "plo_opener", "call_date": CALL_DATE},
    )


def _seed_record(**loan_extra) -> BorrowerRecord:
    loan = {
        "amount_due": 4500,
        "outstanding": 4500,
        "account_ref": "LN-W43-001",
        "last_date_paid": "2026-06-01",
        "committed_date": "2026-08-20",
        "ptp_amount": 4500,
        "customer_name": "रमेश",
        "branch": "कानपुर सिटी",
    }
    loan.update(loan_extra)
    return BorrowerRecord(
        borrower_id=BORROWER,
        identity={"name": "रमेश", "identity_ok": True},
        loan=loan,
        comms_prefs={"phone": "9810587857", "language": "hi-IN"},
        compliance_flags={"tenant_id": TENANT},
    )


def test_simulate_asterisk_startup_fails_loudly():
    settings = Settings(
        stub_mode=True,
        llm_stub=True,
        kb_stub=True,
        tools_mode="simulate",
        carrier="asterisk",
    )
    errors = collect_live_config_errors(settings)
    assert any("TOOLS_MODE=simulate" in e and "asterisk" in e for e in errors)
    with pytest.raises(LiveConfigError, match="simulate"):
        validate_live_configuration(settings)


def test_simulate_without_asterisk_is_allowed():
    settings = Settings(
        stub_mode=True,
        llm_stub=True,
        kb_stub=True,
        tools_mode="simulate",
        carrier="",
    )
    validate_live_configuration(settings)


@pytest.mark.asyncio
async def test_live_timeout_degrades_call_survives():
    transport = _TimeoutTransport()
    tools = LiveToolClient(timeout=0.05, transport=transport, tools_url="http://tools.test")
    memory = InMemoryMemoryStore()
    llm = _ScriptedLLM()
    await handle_turn(
        _req("w43-live", ""),
        memory=memory,
        llm=llm,
        tools=tools,
        kb=_EmptyKB(),
    )
    await handle_turn(
        _req("w43-live", "हाँ, मैं रमेश बोल रहा हूँ।"),
        memory=memory,
        llm=llm,
        tools=tools,
        kb=_EmptyKB(),
    )
    t = await handle_turn(
        _req("w43-live", "abhi kiya QR se pay kar diya"),
        memory=memory,
        llm=llm,
        tools=tools,
        kb=_EmptyKB(),
    )
    spoken = t.reply_text or ""
    assert t.end_call is not True
    assert "अपडेट होने में थोड़ा समय" in spoken
    assert "कानपुर" in spoken
    state = await memory.load_state("w43-live")
    assert state is not None
    assert state.slots.get("payment_claimed") is True
    assert state.slots.get("amount_due") == 4500
    assert state.slots.get("_tool_degraded") is True
    guards = state.slots.get("_last_guards") or {}
    assert guards.get("tool_degraded") is True
    assert int(guards.get("tool_call_ms") or 0) >= 0
    assert transport.calls >= 2


@pytest.mark.asyncio
async def test_stub_returns_seeded_truth():
    memory = InMemoryMemoryStore()
    await memory.save_borrower(_seed_record())
    stub = StubToolClient(source=memory)
    result = await stub.invoke(
        "get_borrower_state",
        {"loan_ref": "LN-W43-001"},
        TENANT,
    )
    payload = result["result"]
    assert result["ok"] is True
    assert payload["found"] is True
    assert payload["outstanding"] == 4500
    assert payload["last_payment"]["date"] == "2026-06-01"
    assert payload["ptp_on_file"]["date"] == "2026-08-20"
    refused = await stub.invoke("hangup_call", {"call_id": "x"}, TENANT)
    assert refused["ok"] is False
    assert refused["error"] == "stub_no_side_effects"


@pytest.mark.asyncio
async def test_stub_payment_claim_refetches():
    memory = InMemoryMemoryStore()
    await memory.save_borrower(_seed_record())
    stub = StubToolClient(source=memory)
    llm = _ScriptedLLM()
    await handle_turn(
        _req("w43-stub", ""),
        memory=memory,
        llm=llm,
        tools=stub,
        kb=_EmptyKB(),
    )
    await handle_turn(
        _req("w43-stub", "हाँ, मैं रमेश बोल रहा हूँ।"),
        memory=memory,
        llm=llm,
        tools=stub,
        kb=_EmptyKB(),
    )
    await memory.save_borrower(
        _seed_record(
            amount_due=0,
            outstanding=0,
            last_date_paid="2026-08-14",
            last_payment_amount=4500,
        )
    )
    t = await handle_turn(
        _req("w43-stub", "abhi kiya QR se pay kar diya"),
        memory=memory,
        llm=llm,
        tools=stub,
        kb=_EmptyKB(),
    )
    spoken = t.reply_text or ""
    assert "अपडेट होने में थोड़ा समय" in spoken
    state = await memory.load_state("w43-stub")
    assert state is not None
    assert state.slots.get("payment_claimed") is True
    assert state.slots.get("amount_due") == 0
    assert state.slots.get("last_date_paid") == "2026-08-14"
    assert state.slots.get("_tool_degraded") is not True
    guards = state.slots.get("_last_guards") or {}
    assert guards.get("tool_degraded") is not True
    assert "tool_call_ms" in guards
    lag = get_fragment(TENANT, "fact_payment_lag")
    assert lag is not None
