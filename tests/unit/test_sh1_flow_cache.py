"""SH-1: flow set cached at startup — no per-turn YAML reparse."""

from pathlib import Path

import pytest

from app.clients.tools_sim import FakeToolClient
from app.engine.turn import handle_turn
from app.flows import loader
from app.flows.loader import FLOWS_DIR, get_flow_set, load_all_flows, reload_flow_set
from app.memory.audit import query_turn_audits_by_borrower
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.schemas.flow import FlowSet
from app.schemas.state import BorrowerRecord
from tests.helpers.scripted_clients import ScriptedKB, ScriptedLLM


@pytest.fixture(autouse=True)
def _reset_flow_cache() -> None:
    loader._FLOW_SET_CACHE = None


def test_get_flow_set_returns_same_object_on_repeated_calls():
    first = get_flow_set()
    second = get_flow_set()
    assert first is second


def test_reload_flow_set_replaces_cached_object():
    original = get_flow_set()
    reloaded = reload_flow_set()
    assert reloaded is not original
    assert get_flow_set() is reloaded


@pytest.mark.asyncio
async def test_handle_turn_does_not_reparse_after_cache_warm(monkeypatch: pytest.MonkeyPatch):
    call_count = {"n": 0}
    real_load = load_all_flows

    def counting_load(flows_dir: Path = FLOWS_DIR) -> FlowSet:
        call_count["n"] += 1
        return real_load(flows_dir)

    monkeypatch.setattr(loader, "load_all_flows", counting_load)

    warmed = get_flow_set()
    assert call_count["n"] == 1

    memory = InMemoryMemoryStore()
    kb = ScriptedKB([])
    llm = ScriptedLLM([])
    tools = FakeToolClient()

    request = TurnRequest(
        call_id="call-sh1-noreparse",
        tenant_id="default",
        borrower_id="borrower-sh1-noreparse",
        transcript="hello",
        turn_meta={"call_date": "2026-06-25"},
    )

    for _ in range(3):
        await handle_turn(
            request,
            memory=memory,
            kb=kb,
            llm=llm,
            tools=tools,
            flows=warmed,
        )

    for _ in range(2):
        await handle_turn(
            request,
            memory=memory,
            kb=kb,
            llm=llm,
            tools=tools,
        )

    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_handle_turn_behavior_unchanged_with_cached_flows():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:promise_to_pay]] kal"}])
    llm_baseline = ScriptedLLM(
        [
            [
                {"command": "start_flow", "flow": "promise_to_pay"},
                {"command": "set_slot", "name": "ptp_date", "value": "2026-06-27"},
            ]
        ]
    )
    llm_cached = ScriptedLLM(
        [
            [
                {"command": "start_flow", "flow": "promise_to_pay"},
                {"command": "set_slot", "name": "ptp_date", "value": "2026-06-27"},
            ]
        ]
    )
    tools = FakeToolClient()
    await memory.save_borrower(
        BorrowerRecord(
            borrower_id="borrower-sh1-behavior",
            loan={"amount_due": 5000, "dpd": 45, "bucket": "30-60"},
            identity={"identity_ok": True},
        )
    )

    request = TurnRequest(
        call_id="call-sh1-behavior",
        tenant_id="default",
        borrower_id="borrower-sh1-behavior",
        transcript="kal de dunga",
        turn_meta={"call_date": "2026-06-25"},
    )

    baseline = await handle_turn(
        request,
        memory=memory,
        kb=kb,
        llm=llm_baseline,
        tools=tools,
        flows=load_all_flows(),
    )

    memory2 = InMemoryMemoryStore()
    await memory2.save_borrower(
        BorrowerRecord(
            borrower_id="borrower-sh1-behavior",
            loan={"amount_due": 5000, "dpd": 45, "bucket": "30-60"},
            identity={"identity_ok": True},
        )
    )

    cached = await handle_turn(
        request,
        memory=memory2,
        kb=kb,
        llm=llm_cached,
        tools=tools,
        flows=get_flow_set(),
    )

    assert cached.reply_text == baseline.reply_text
    assert cached.transfer_to_human == baseline.transfer_to_human
    assert "schedule_followup" in cached.actions_executed

    audits = await query_turn_audits_by_borrower(memory2, "borrower-sh1-behavior")
    assert audits[0].gate_verdict == "allow"
