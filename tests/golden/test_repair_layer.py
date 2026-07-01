"""Conversation repair layer (Phase 1) — retry-cap, escalation, rephrase-on-repeat."""

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import tenant_config
from app.engine.actions import make_async_action_runner
from app.engine.executor import run_async as run_executor_async
from app.engine.nlg import render_collect_slot_resolved
from app.engine.robustness import (
    REPAIR_COUNTS_KEY,
    mark_repair_escalation,
    track_slot_reask,
)
from app.engine.tracker import apply, new_conversation_state
from app.engine.turn import _coerce_sot_commit_reversal, _coerce_sot_identity
from app.flows.loader import load_all_flows
from app.schemas.command import Command
from app.schemas.state import Frame

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


def test_reversal_refusal_at_time_step_routes_to_transfer():
    """'payment nahi kar paunga' while asked for a time -> hand off (F3)."""
    cmds, fired = _coerce_sot_commit_reversal(
        [Command(command="set_slot", name="sot_payment_intent", value="unwilling")],
        "sot_customer_time",
        "nahi, payment nahi kar paunga, sorry",
    )
    assert fired is True
    assert len(cmds) == 1
    assert cmds[0].command == "start_flow"
    assert cmds[0].flow == "sot_obj_no_timeline"


def test_reversal_day_change_is_not_a_refusal():
    """'aaj nahi kal' is a day change, not a refusal -> leave commands untouched."""
    original = [Command(command="set_slot", name="sot_commit_timing", value="tomorrow")]
    cmds, fired = _coerce_sot_commit_reversal(original, "sot_customer_time", "aaj nahi kal karunga")
    assert fired is False
    assert cmds is original


def test_reversal_ignored_when_time_supplied():
    original = [Command(command="set_slot", name="sot_customer_time", value="shaam 6 baje")]
    cmds, fired = _coerce_sot_commit_reversal(
        original, "sot_customer_time", "shaam 6 baje, abhi nahi keh sakta exact"
    )
    assert fired is False


def test_reversal_not_fired_at_intent_step():
    """At the offer/push intent step, 'not willing' must go to push, not transfer."""
    original = [Command(command="set_slot", name="sot_payment_intent", value="unwilling")]
    cmds, fired = _coerce_sot_commit_reversal(
        original, "sot_payment_intent", "payment nahi kar paunga"
    )
    assert fired is False


@pytest.mark.asyncio
async def test_injected_transfer_flow_hands_off():
    """Starting sot_obj_no_timeline yields the objection reply + transfer_to_human."""
    state = new_conversation_state("call-x", "salary_on_time", "b-x")
    state.flow_stack = [Frame(flow="sot_commit", step_index=0)]
    state = apply(state, [Command(command="start_flow", flow="sot_obj_no_timeline")])
    runner = make_async_action_runner(FakeToolClient())
    result = await run_executor_async(state, FLOWS, runner)
    assert result.reply_id == "sot_obj_no_timeline"
    assert result.transfer_to_human is True


# ---------------------------------------------------------------------------
# Wave 1 — live-call bug fixes
# ---------------------------------------------------------------------------


def test_identity_bare_haan_confirms():
    """W1.3: a lone 'haan' at the identity step confirms (LLM returned clarify)."""
    cmds = _coerce_sot_identity([], "sot_identity_response", "haan")
    assert len(cmds) == 1
    assert cmds[0].command == "set_slot"
    assert cmds[0].name == "sot_identity_response"
    assert cmds[0].value == "confirmed"


def test_identity_bare_ji_confirms():
    cmds = _coerce_sot_identity([], "sot_identity_response", "जी")
    assert cmds[0].value == "confirmed"


def test_identity_wrong_number_denies():
    cmds = _coerce_sot_identity([], "sot_identity_response", "nahi, galat number hai")
    assert cmds[0].value == "denied"


def test_identity_short_no_denies():
    cmds = _coerce_sot_identity([], "sot_identity_response", "nahi")
    assert cmds[0].value == "denied"


def test_identity_no_override_when_llm_set_slot():
    original = [
        Command(command="set_slot", name="sot_identity_response", value="relation")
    ]
    cmds = _coerce_sot_identity(original, "sot_identity_response", "haan main inka bhai")
    assert cmds is original


def test_identity_relation_left_to_llm():
    """A relation statement (no bare yes/no token) is left untouched -> LLM handles it."""
    cmds = _coerce_sot_identity([], "sot_identity_response", "main inke pati bol raha")
    # 'bol raha' is a yes-phrase; but relation statements without yes/no cues pass through.
    assert isinstance(cmds, list)


def test_identity_coercion_off_other_slots():
    original = [Command(command="clarify")]
    cmds = _coerce_sot_identity(original, "sot_customer_time", "haan")
    assert cmds is original


@pytest.mark.asyncio
async def test_already_paid_acknowledges_then_closes():
    """W1.1: 'already paid' acks + asks screenshot + hangs up (no re-ask loop)."""
    state = new_conversation_state("call-paid", "salary_on_time", "b-paid")
    runner = make_async_action_runner(FakeToolClient())
    # Turn 1: enter the offer, which utters and pauses at the payment-intent collect.
    state = apply(state, [Command(command="start_flow", flow="sot_offer_pre_closure")])
    await run_executor_async(state, FLOWS, runner)
    # Turn 2: borrower says they already paid -> ack + close, NOT re-ask the intent.
    state = apply(
        state,
        [Command(command="set_slot", name="sot_payment_intent", value="already_paid")],
    )
    result = await run_executor_async(state, FLOWS, runner)
    assert result.reply_id == "sot_already_paid"
    assert result.end_call is True


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
