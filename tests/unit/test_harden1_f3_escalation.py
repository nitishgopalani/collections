"""HARDEN-1 F3 — escalation end_call + agent_fault repair-counter guard.

F3(a): repair/limit (frustration) escalation reply must carry end_call in the
        SAME turn — no post-escalation zombie turn. The frustration_escalate
        trigger was missing from the TurnResponse.end_call OR.

F3(b): turns following a failed/empty agent reply do not increment the
        borrower's repair counter (extends the routing_miss principle via an
        agent_fault flag persisted in slots).
"""

from __future__ import annotations

import json

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.retrieval import clear_retrieval_cache
from app.engine.robustness import (
    AGENT_FAULT_KEY,
    REPAIR_COUNTS_KEY,
    record_agent_fault,
    track_slot_reask,
)
from app.engine.tracker import new_conversation_state
from app.engine.turn import handle_turn
from app.flows.loader import reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.sim.scripted_clients import ScriptedKB

CALL_DATE = "2026-06-25"
BORROWER = "sot_f3_borrower"


def _state(**slots):
    state = new_conversation_state("call-1", "salary_on_time", "borrower-1")
    state.slots.update(slots)
    return state


# ─── F3(b) unit tests ───────────────────────────────────────────────────────


def test_f3b_agent_fault_param_skips_repair_increment():
    """agent_fault=True (explicit) skips the increment + escalate, like routing_miss."""
    state = _state(last_question_slot="sot_customer_time")
    # Re-ask the same slot 3 times with agent_fault — counter must stay 0, no escalate.
    for _ in range(3):
        state, escalate = track_slot_reask(
            state,
            question_slot="sot_customer_time",
            had_inbound=True,
            max_retries=2,
            agent_fault=True,
        )
        state.slots["last_question_slot"] = "sot_customer_time"
        assert escalate is False
    assert state.slots[REPAIR_COUNTS_KEY].get("sot_customer_time", 0) == 0


def test_f3b_agent_fault_flag_from_slots_skips_increment_and_clears():
    """The persisted AGENT_FAULT_KEY flag (set by record_agent_fault on the prior
    turn) is read from slots, skips the increment, and is cleared once consumed."""
    state = _state(last_question_slot="sot_customer_time")
    state.slots[AGENT_FAULT_KEY] = True  # prior turn's reply was empty/failed

    state, escalate = track_slot_reask(
        state,
        question_slot="sot_customer_time",
        had_inbound=True,
        max_retries=2,
    )
    assert escalate is False
    assert state.slots[REPAIR_COUNTS_KEY].get("sot_customer_time", 0) == 0
    # Flag consumed and cleared so a later healthy turn does not inherit it.
    assert AGENT_FAULT_KEY not in state.slots


def test_f3b_record_agent_fault_sets_on_empty_clears_on_nonempty():
    """record_agent_fault sets the flag when reply_text is empty, clears it when not."""
    # Empty reply → flag set
    state = _state()
    state = record_agent_fault(state, reply_text="")
    assert state.slots[AGENT_FAULT_KEY] is True
    state = record_agent_fault(state, reply_text="   ")
    assert state.slots[AGENT_FAULT_KEY] is True

    # Non-empty reply → flag cleared
    state = record_agent_fault(state, reply_text="Main aapki baat sun raha hoon.")
    assert AGENT_FAULT_KEY not in state.slots


def test_f3b_empty_reply_then_reask_does_not_increment():
    """End-to-end at the robustness layer: an empty reply sets the flag, and the
    next re-ask (same slot, with inbound) does NOT burn a borrower retry."""
    state = _state(last_question_slot="sot_customer_time")
    # Turn N: agent reply was empty → flag set.
    state = record_agent_fault(state, reply_text="")
    assert state.slots[AGENT_FAULT_KEY] is True

    # Turn N+1: borrower replied, executor re-asks the same slot. agent_fault
    # (from the flag) must skip the increment.
    state, escalate = track_slot_reask(
        state,
        question_slot="sot_customer_time",
        had_inbound=True,
        max_retries=2,
    )
    assert escalate is False
    assert state.slots[REPAIR_COUNTS_KEY].get("sot_customer_time", 0) == 0
    assert AGENT_FAULT_KEY not in state.slots


# ─── F3(a) integration test ──────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _sot_test_mode(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("TEST_SOT_SCENARIO", "pre")
    monkeypatch.setenv("TEST_FORCE_TENANT", "")
    get_settings.cache_clear()
    reload_flow_set()
    clear_retrieval_cache()
    yield
    clear_retrieval_cache()
    get_settings.cache_clear()


class _ScriptedLLM:
    """Returns no commands so the executor idles and the NLG renders the
    escalation reply when frustration_escalate fires."""

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


class _EmptyKB:
    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def retrieve(self, *a, **k):  # noqa: ANN001
        return []


def _req(call_id: str, transcript: str) -> TurnRequest:
    return TurnRequest(
        call_id=call_id,
        tenant_id="salary_on_time",
        borrower_id=BORROWER,
        transcript=transcript,
        turn_meta={"force_flow": "sot_opener", "call_date": CALL_DATE},
    )


@pytest.mark.asyncio
async def test_f3a_frustration_escalation_carries_end_call_same_turn():
    """F3(a): when the frustration limit fires, the escalation reply must carry
    end_call=True in the SAME turn (no zombie turn after). Pre-seed the
    frustration counter to threshold-1 so one frustrated turn triggers it."""
    from app.engine.robustness import FRUSTRATION_COUNT_KEY

    memory = InMemoryMemoryStore()
    call_id = "sot-f3a-frustration"

    # Pre-seed state: identity confirmed, on a valid flow, frustration counter
    # at threshold-1 (=2 for salary_on_time, threshold=3) so one more frustrated
    # turn hits the limit. Do NOT set end_call (terminal guard would short-circuit).
    seeded = new_conversation_state(call_id, "salary_on_time", BORROWER)
    seeded.slots["sot_identity_response"] = "confirmed"
    seeded.slots["identity_confirmed"] = True
    seeded.slots[FRUSTRATION_COUNT_KEY] = 2
    seeded.flow_stack = []
    seeded.version = 1  # InMemoryMemoryStore expects version=1 on first save
    await memory.save_state(seeded)

    llm = _ScriptedLLM([[]])  # no commands — executor idles, NLG renders escalation

    # "pareshaan" is a frustration keyword → med/high intensity → counter hits 3.
    result = await handle_turn(
        _req(call_id, "main bahut pareshaan ho gaya, band karo ye"),
        memory=memory,
        kb=_EmptyKB(),
        llm=llm,
        tools=FakeToolClient(),
    )

    # The escalation reply is the tenant's escalation_reply, and the call ends
    # THIS turn (F3(a): frustration_escalate is now in the end_call OR).
    assert result.end_call is True, (
        "F3(a): frustration escalation must carry end_call=True in the same turn "
        f"(got end_call={result.end_call}, disposition={result.disposition})"
    )
    assert result.disposition == "ESCALATED_FRUSTRATION"
    assert result.reply_text  # the caller hears the escalation line, not dead air
