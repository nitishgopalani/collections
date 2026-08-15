"""P2.4 — Tier-2 catalog routing goldens (scripted tenant, no pinning/floor)."""

import json

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.retrieval import clear_retrieval_cache
from app.engine.turn import handle_turn
from app.flows.loader import reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.sim.scripted_clients import ScriptedKB

CALL_DATE = "2026-06-25"
BORROWER = "sot_test_borrower"


@pytest.fixture(autouse=True)
def _catalog_mode(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("SCRIPTED_CATALOG_ROUTING", "true")
    monkeypatch.setenv("SOT_DIGRESSION", "false")
    monkeypatch.setenv("TEST_SOT_SCENARIO", "pre")
    monkeypatch.setenv("CALL_WINDOW_START", "00:00")
    monkeypatch.setenv("CALL_WINDOW_END", "23:59")
    get_settings.cache_clear()
    reload_flow_set()
    clear_retrieval_cache()
    yield
    clear_retrieval_cache()
    get_settings.cache_clear()


class _EmptyKB:
    retrieve_calls = 0

    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def retrieve(self, text, tenant_id, k: int = 6):
        type(self).retrieve_calls += 1
        return []


class _ScriptedLLM:
    def __init__(self, turns):
        self._responses = [json.dumps(t) for t in turns]
        self.call_count = 0
        self.user_prompts: list[str] = []

    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def complete(self, system: str, user: str, *, json_only: bool = True, **kw) -> str:
        self.user_prompts.append(user)
        self.call_count += 1
        if self.call_count <= len(self._responses):
            return self._responses[self.call_count - 1]
        return "[]"


def _req(call_id: str, transcript: str) -> TurnRequest:
    return TurnRequest(
        call_id=call_id,
        tenant_id="salary_on_time",
        borrower_id=BORROWER,
        transcript=transcript,
        turn_meta={"force_flow": "sot_opener", "call_date": CALL_DATE},
    )


async def _run(memory, llm, kb, call_id, transcript):
    return await handle_turn(
        _req(call_id, transcript),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=FakeToolClient(),
    )


@pytest.mark.asyncio
async def test_never_loan_routes_turn1_without_accumulator():
    """'loan hai hi nahi' on the offer step → sot_obj_never_loan immediately."""
    memory = InMemoryMemoryStore()
    call_id = "p2-never-loan"
    kb = _EmptyKB()
    llm = _ScriptedLLM(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [],  # coercion must route dispute; no LLM start_flow / no accumulator
        ]
    )
    await _run(memory, llm, kb, call_id, "")
    await _run(memory, llm, kb, call_id, "haan Rishabh")
    r3 = await _run(memory, llm, kb, call_id, "loan hai hi nahi")
    assert r3.reply_id == "sot_obj_never_loan"
    state = await memory.load_state(call_id)
    assert any(f.flow == "sot_obj_never_loan" for f in state.flow_stack)
    assert state.slots.get("_dispute_evidence", {}).get("sot_obj_never_loan", 0) in {
        0,
        None,
    } or state.slots.get("_dispute_evidence", {}).get("sot_obj_never_loan", 0) == 0


@pytest.mark.asyncio
async def test_kaise_pay_karun_routes_link_without_pinning():
    """Catalog includes link_request; LLM start_flow works with digression/pinning off."""
    memory = InMemoryMemoryStore()
    call_id = "p2-link-catalog"
    kb = _EmptyKB()
    llm = _ScriptedLLM(
        [
            # t1 opener skip + t2 identity D1 cue-hit skip. First complete() is t3.
            [{"command": "start_flow", "flow": "sot_obj_link_request"}],
        ]
    )
    await _run(memory, llm, kb, call_id, "")
    await _run(memory, llm, kb, call_id, "haan Rishabh")
    calls_before = kb.retrieve_calls
    r3 = await _run(memory, llm, kb, call_id, "kaise pay karun")
    assert kb.retrieve_calls == calls_before  # catalog mode: never retrieve
    prompt = json.loads(llm.user_prompts[-1])
    assert prompt.get("routing_note")
    names = [c["name"] for c in prompt["candidate_flows"]]
    assert "sot_obj_link_request" in names
    assert all("score" not in c for c in prompt["candidate_flows"])
    assert r3.reply_id == "sot_obj_link_request"


@pytest.mark.asyncio
async def test_deflection_busy_excluded_while_awaiting_payment_intent():
    """Busy excuse at sot_payment_intent_2 must not be startable from the catalog."""
    memory = InMemoryMemoryStore()
    call_id = "p2-busy-suppress"
    kb = ScriptedKB([])
    # offer refuse → push reason → intent_2 → busy digression attempt
    llm = _ScriptedLLM(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [{"command": "set_slot", "name": "sot_payment_intent", "value": "refused"}],
            [{"command": "set_slot", "name": "sot_payment_problem", "value": "salary_delay"}],
            [{"command": "start_flow", "flow": "sot_obj_busy"}],
        ]
    )
    await _run(memory, llm, kb, call_id, "")
    await _run(memory, llm, kb, call_id, "haan Rishabh")
    await _run(memory, llm, kb, call_id, "aaj nahi kar paunga")
    await _run(memory, llm, kb, call_id, "salary late hai")
    state = await memory.load_state(call_id)
    awaiting = None
    if state.flow_stack:
        from app.flows.loader import get_flow_set

        frame = state.flow_stack[-1]
        flow = get_flow_set().flows.get(frame.flow)
        if flow and frame.step_index < len(flow.steps):
            awaiting = flow.steps[frame.step_index].collect
    assert awaiting == "sot_payment_intent_2"

    r5 = await _run(memory, llm, kb, call_id, "main busy hun baad mein baat karo")
    prompt = json.loads(llm.user_prompts[-1])
    names = {c["name"] for c in prompt["candidate_flows"]}
    assert "sot_obj_busy" not in names
    assert "sot_obj_never_loan" in names  # disputes stay
    assert "sot_obj_link_request" in names  # info stays
    assert r5.reply_id != "sot_obj_busy"
    state2 = await memory.load_state(call_id)
    assert not any(f.flow == "sot_obj_busy" for f in state2.flow_stack)


@pytest.mark.asyncio
async def test_catalog_mode_skips_kb_even_with_digression_flag(monkeypatch):
    monkeypatch.setenv("SOT_DIGRESSION", "true")
    get_settings.cache_clear()
    memory = InMemoryMemoryStore()
    call_id = "p2-no-kb"
    kb = _EmptyKB()
    llm = _ScriptedLLM(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [],
        ]
    )
    await _run(memory, llm, kb, call_id, "")
    await _run(memory, llm, kb, call_id, "haan Rishabh")
    before = kb.retrieve_calls
    await _run(memory, llm, kb, call_id, "kaise pay karun")
    assert kb.retrieve_calls == before
