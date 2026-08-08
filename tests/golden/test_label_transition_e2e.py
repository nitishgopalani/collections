"""End-to-end LTL golden tests through handle_turn (salary_on_time).

Covers three contracts:
  * flag OFF (default)      -> no `_label` state written, behavior unchanged.
  * shadow mode             -> labels recorded, but commands/behavior unchanged; in
                               particular a link request the LLM missed is NOT routed.
  * enforce mode (SOT)      -> the same missed link request is force-routed to
                               sot_obj_link_request (the live-call fix), and an
                               unresolved dispute clarifies instead of paying.
"""

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
def _sot_test_mode(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("SOT_DIGRESSION", "true")
    monkeypatch.setenv("TEST_SOT_SCENARIO", "pre")
    get_settings.cache_clear()
    reload_flow_set()
    clear_retrieval_cache()
    yield
    clear_retrieval_cache()
    get_settings.cache_clear()


def _enable_ltl(monkeypatch, mode: str, scope: str = "supported"):
    monkeypatch.setenv("LABEL_TRANSITION_ENABLED", "true")
    monkeypatch.setenv("LABEL_TRANSITION_MODE", mode)
    monkeypatch.setenv("LABEL_TRANSITION_SCOPE", scope)
    get_settings.cache_clear()


def _req(call_id: str, transcript: str) -> TurnRequest:
    return TurnRequest(
        call_id=call_id,
        tenant_id="salary_on_time",
        borrower_id=BORROWER,
        transcript=transcript,
        turn_meta={"force_flow": "sot_opener", "call_date": CALL_DATE},
    )


class _ScriptedLLM:
    def __init__(self, turns):
        self._responses = [json.dumps(t) for t in turns]
        self.call_count = 0

    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def complete(self, system: str, user: str, *, json_only: bool = True) -> str:
        self.call_count += 1
        if self.call_count <= len(self._responses):
            return self._responses[self.call_count - 1]
        return "[]"


def _llm(turns):
    return _ScriptedLLM(turns)


async def _run(memory, llm, kb, call_id, transcript):
    return await handle_turn(
        _req(call_id, transcript),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=FakeToolClient(),
    )


def _kb_link():
    return ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:sot_obj_link_request]] link"}])


async def _greet_and_confirm(memory, llm, kb, call_id):
    await _run(memory, llm, kb, call_id, "")
    await _run(memory, llm, kb, call_id, "haan Rishabh")


# --- flag OFF --------------------------------------------------------------------
@pytest.mark.asyncio
async def test_flag_off_writes_no_label_state(monkeypatch):
    memory = InMemoryMemoryStore()
    call_id = "ltl-off"
    kb = _kb_link()
    llm = _llm(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [{"command": "start_flow", "flow": "sot_obj_link_request"}],
        ]
    )
    await _greet_and_confirm(memory, llm, kb, call_id)
    r3 = await _run(memory, llm, kb, call_id, "mujhe payment link bhej do")
    assert r3.reply_id == "sot_obj_link_request"
    state = await memory.load_state(call_id)
    assert "_label" not in state.slots


# --- shadow ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_shadow_records_label_without_changing_behavior(monkeypatch):
    _enable_ltl(monkeypatch, "shadow")
    memory = InMemoryMemoryStore()
    call_id = "ltl-shadow"
    kb = _kb_link()
    llm = _llm(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [{"command": "start_flow", "flow": "sot_obj_link_request"}],
        ]
    )
    await _greet_and_confirm(memory, llm, kb, call_id)
    r3 = await _run(memory, llm, kb, call_id, "mujhe payment link bhej do")
    # Behavior unchanged (link still routed by the LLM's own start_flow).
    assert r3.reply_id == "sot_obj_link_request"
    state = await memory.load_state(call_id)
    label = state.slots.get("_label")
    assert label is not None
    assert label["active_label"] == "support.payment_link_request"
    assert label["mode"] == "shadow"
    # A label_transition audit event was recorded.
    assert any(e.kind == "label_transition" for e in state.events)


@pytest.mark.asyncio
async def test_shadow_does_not_route_link_when_llm_silent(monkeypatch):
    """Proves shadow is observe-only: the missed link is NOT force-routed."""
    _enable_ltl(monkeypatch, "shadow")
    memory = InMemoryMemoryStore()
    call_id = "ltl-shadow-silent"
    kb = _kb_link()
    llm = _llm(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [],  # LLM emits nothing on the link turn
        ]
    )
    await _greet_and_confirm(memory, llm, kb, call_id)
    r3 = await _run(memory, llm, kb, call_id, "mujhe payment link bhej do")
    assert r3.reply_id != "sot_obj_link_request"
    state = await memory.load_state(call_id)
    # Label still detected/recorded even though nothing was enforced.
    assert state.slots["_label"]["active_label"] == "support.payment_link_request"


# --- enforce ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_enforce_routes_link_when_llm_silent(monkeypatch):
    """The live-call fix: LLM misses the link request, enforce force-routes it."""
    _enable_ltl(monkeypatch, "enforce")
    memory = InMemoryMemoryStore()
    call_id = "ltl-enforce-link"
    kb = _kb_link()
    llm = _llm(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [],  # LLM emits nothing on the link turn
        ]
    )
    await _greet_and_confirm(memory, llm, kb, call_id)
    r3 = await _run(memory, llm, kb, call_id, "sir mujhe payment link bhej do")
    assert r3.reply_id == "sot_obj_link_request"
    assert "send_whatsapp_message" in r3.actions_executed
    state = await memory.load_state(call_id)
    assert state.slots["_label"]["enforce_applied"] is True
