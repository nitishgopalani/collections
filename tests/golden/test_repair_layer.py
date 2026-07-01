"""Conversation repair layer (Phase 1) — retry-cap, escalation, rephrase-on-repeat."""

from app.config import tenant_config
from app.engine.nlg import render_collect_slot_resolved
from app.engine.robustness import (
    REPAIR_COUNTS_KEY,
    mark_repair_escalation,
    track_slot_reask,
)
from app.engine.tracker import new_conversation_state
from app.flows.loader import load_all_flows

FLOWS = load_all_flows()


def _state(**slots):
    state = new_conversation_state("call-1", "salary_on_time", "borrower-1")
    state.slots.update(slots)
    return state


def test_reask_increments_then_escalates():
    """Same slot re-asked after a caller reply counts up; escalates past the cap."""
    # First ask: no prior slot, an inbound reply is present but slot is new.
    state = _state(last_question_slot="sot_customer_time")

    # Re-ask #1 (cap=2): counts to 1, no escalation.
    state, escalate = track_slot_reask(
        state, question_slot="sot_customer_time", had_inbound=True, max_retries=2
    )
    assert escalate is False
    assert state.slots[REPAIR_COUNTS_KEY]["sot_customer_time"] == 1

    # Keep last_question_slot pinned (record_outbound_context would do this live).
    state.slots["last_question_slot"] = "sot_customer_time"
    # Re-ask #2: counts to 2, still no escalation.
    state, escalate = track_slot_reask(
        state, question_slot="sot_customer_time", had_inbound=True, max_retries=2
    )
    assert escalate is False
    assert state.slots[REPAIR_COUNTS_KEY]["sot_customer_time"] == 2

    state.slots["last_question_slot"] = "sot_customer_time"
    # Would-be re-ask #3: cap reached → escalate, no further increment.
    state, escalate = track_slot_reask(
        state, question_slot="sot_customer_time", had_inbound=True, max_retries=2
    )
    assert escalate is True
    assert state.slots[REPAIR_COUNTS_KEY]["sot_customer_time"] == 2


def test_reask_resets_when_flow_advances():
    state = _state(
        last_question_slot="sot_customer_time",
        **{REPAIR_COUNTS_KEY: {"sot_customer_time": 2}},
    )
    state, escalate = track_slot_reask(
        state, question_slot="sot_final_confirm", had_inbound=True, max_retries=2
    )
    assert escalate is False
    assert state.slots[REPAIR_COUNTS_KEY].get("sot_customer_time") is None
    assert state.slots[REPAIR_COUNTS_KEY]["sot_final_confirm"] == 0


def test_no_inbound_does_not_count():
    """Opener / silent turns must not accrue re-asks."""
    state = _state(last_question_slot="sot_identity_response")
    state, escalate = track_slot_reask(
        state, question_slot="sot_identity_response", had_inbound=False, max_retries=2
    )
    assert escalate is False
    assert state.slots.get(REPAIR_COUNTS_KEY, {}).get("sot_identity_response", 0) == 0


def test_mark_repair_escalation_closes_call():
    state = _state(**{REPAIR_COUNTS_KEY: {"sot_customer_time": 2}})
    state = mark_repair_escalation(state, question_slot="sot_customer_time")
    assert state.slots["disposition"] == "ESCALATED_UNCLEAR"
    assert state.slots["end_call"] is True
    assert state.slots["sot_call_closed"] is True
    assert state.slots[REPAIR_COUNTS_KEY] == {}


def test_reask_variant_rotates_by_repair_count():
    """Each re-ask of a slot must sound different (rephrase-on-repeat, F2)."""
    tenant_cfg = tenant_config("salary_on_time")
    seen = set()
    for count in (0, 1, 2):
        state = _state(**{REPAIR_COUNTS_KEY: {"sot_customer_time": count}})
        reply = render_collect_slot_resolved(
            "sot_customer_time", state, FLOWS, tenant_cfg=tenant_cfg
        )
        seen.add(reply.text)
    # sot_ask_time has three authored variants — none should repeat across re-asks.
    assert len(seen) == 3
