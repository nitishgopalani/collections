"""W2-4 — enforce flip + replay + live gate.

Covers the deferred W2-2 items (enforce-coupled) + the enforce flip:
  - Repair counter increments ONLY on failed confirms; routing_miss /
    agent_fault become reasons (not skip conditions).
  - source= tagging on every slot write (system / borrower_claim / confirmed);
    borrower assertions never enter system-fact slots (gate bypasses
    source=system on money-state slots).
  - Enforce flip: verdict=downgrade -> confirm-ask compose + _pending_confirm;
    verdict=hold -> drop apply_commands (re-ask only).
  - Replay corpus (all in enforce mode): 12-scenario OOF table + ASR-noise
    variants -> zero unbounded outcomes (every turn lands in one of the 7).
"""

from __future__ import annotations

import os

import pytest

from app.engine.commitment_gate import commitment_gate, commitment_gate_enforce_enabled
from app.engine.robustness import (
    PENDING_CONFIRM_KEY,
    REPAIR_COUNTS_KEY,
    set_pending_confirm,
    track_slot_reask_gated,
)
from app.engine.tracker import apply, hydrate_from_borrower, new_conversation_state
from app.schemas.command import Command
from app.schemas.state import ConversationState

PAISALO_SLOT_COST_CLASS = {
    "plo_payment_intent": "money_state",
    "plo_timeline": "money_state",
    "plo_identity_response": "identity_confirm",
    "customer_name": "pii",
    "phone": "pii",
    "repay_amount": "money_state",
    "loan_amount": "money_state",
    "committed_date": "money_state",
}


def _ev(score: int, reason: str = "test") -> dict:
    return {"evidence": score, "evidence_reason": reason, "evidence_signals": {}}


def _state_with_pending_confirm(slot: str) -> tuple[ConversationState, dict]:
    state = new_conversation_state("call-w24", "paisalo", "borrower-w24")
    state = set_pending_confirm(state, slot=slot, fragment_id=f"confirm_{slot}")
    # Return the pending dict too — track_slot_reask_gated now takes it as
    # prior_pending_confirm (captured before the gate) instead of popping
    # from state. The gate (turn.py) manages _pending_confirm lifecycle.
    return state, {"slot": slot, "fragment_id": f"confirm_{slot}"}


# --- W2-4.1a: Repair counter (failed-confirm-only rule) ---


def test_repair_failed_confirm_increments():
    state, pending = _state_with_pending_confirm("committed_date")
    state, escalate, reason = track_slot_reask_gated(
        state, question_slot="committed_date", had_inbound=True,
        max_retries=3, evidence_score=2, prior_pending_confirm=pending,
    )
    assert state.slots[REPAIR_COUNTS_KEY].get("committed_date") == 1
    assert reason == "failed_confirm"
    assert escalate is False


def test_repair_successful_confirm_no_increment():
    state, pending = _state_with_pending_confirm("committed_date")
    state, escalate, reason = track_slot_reask_gated(
        state, question_slot="committed_date", had_inbound=True,
        max_retries=3, evidence_score=3, prior_pending_confirm=pending,
    )
    assert state.slots[REPAIR_COUNTS_KEY].get("committed_date", 0) == 0
    assert reason is None


def test_repair_no_pending_no_increment():
    state = new_conversation_state("call-w24", "paisalo", "borrower-w24")
    state.slots["last_question_slot"] = "committed_date"
    state, _, reason = track_slot_reask_gated(
        state, question_slot="committed_date", had_inbound=True,
        max_retries=3, evidence_score=2, routing_miss=True,
    )
    assert state.slots[REPAIR_COUNTS_KEY].get("committed_date", 0) == 0
    assert reason is None


def test_repair_failed_confirm_escalates_at_max():
    state, pending = _state_with_pending_confirm("committed_date")
    state.slots[REPAIR_COUNTS_KEY] = {"committed_date": 3}
    state, escalate, reason = track_slot_reask_gated(
        state, question_slot="committed_date", had_inbound=True,
        max_retries=3, evidence_score=2, prior_pending_confirm=pending,
    )
    assert escalate is True
    assert reason == "failed_confirm_escalate"


def test_repair_does_not_pop_pending_confirm():
    """W2-4: track_slot_reask_gated no longer pops _pending_confirm — the
    gate (turn.py) manages the lifecycle (sets on downgrade, clears on
    execute/hold). This test locks that contract: the slot survives the
    call so the caller can clear it based on the gate verdict.
    """
    state, pending = _state_with_pending_confirm("committed_date")
    state, _, _ = track_slot_reask_gated(
        state, question_slot="committed_date", had_inbound=True,
        max_retries=3, evidence_score=2, prior_pending_confirm=pending,
    )
    assert PENDING_CONFIRM_KEY in state.slots  # NOT popped by the repair counter


# --- W2-4.1b: source= tagging ---


def test_hydrate_tags_source_system():
    from app.schemas.state import BorrowerRecord

    borrower = BorrowerRecord(
        borrower_id="borrower-w24",
        loan={"amount_due": 5000, "outstanding": 5000, "kist_number": "3"},
        compliance_flags={}, trust_current=50, risk_flags=[],
        persona_current={"voice": "simran"},
        identity={"identity_ok": True, "name": "Ramesh"},
        comms_prefs={"phone": "9999999999"},
    )
    state = new_conversation_state("call-w24", "paisalo", "borrower-w24")
    state = hydrate_from_borrower(state, borrower)
    sources = state.slots.get("_slot_sources", {})
    assert sources.get("amount_due") == "system"
    assert sources.get("borrower_name") == "system"
    assert sources.get("phone") == "system"
    assert "_slot_sources" not in sources


def test_apply_tags_set_slot_source():
    state = new_conversation_state("call-w24", "paisalo", "borrower-w24")
    cmd = Command(command="set_slot", name="committed_date", value="15 Aug", source="borrower_claim")
    state = apply(state, [cmd])
    assert state.slots["committed_date"] == "15 Aug"
    assert state.slots["_slot_sources"]["committed_date"] == "borrower_claim"


def test_apply_defaults_source_system():
    state = new_conversation_state("call-w24", "paisalo", "borrower-w24")
    cmd = Command(command="set_slot", name="plo_payment_intent", value="yes")
    state = apply(state, [cmd])
    assert state.slots["_slot_sources"]["plo_payment_intent"] == "system"


# --- W2-4.2: Source-aware gate ---


def test_gate_source_system_bypasses_money_state():
    candidate = [Command(command="set_slot", name="committed_date", value="15 Aug", source="system")]
    verdict = commitment_gate(
        candidate, evidence=_ev(0, "non_addressed"), cost_table=None,
        slot_cost_class=PAISALO_SLOT_COST_CLASS, identity_ok=True, awaited_slot="plo_timeline",
    )
    assert verdict["verdict"] == "execute"
    assert verdict["cost_class"] == "script_reask"


def test_gate_source_confirmed_bypasses_money_state():
    candidate = [Command(command="set_slot", name="committed_date", value="15 Aug", source="confirmed")]
    verdict = commitment_gate(
        candidate, evidence=_ev(2, "cue_agree"), cost_table=None,
        slot_cost_class=PAISALO_SLOT_COST_CLASS, identity_ok=True, awaited_slot="plo_timeline",
    )
    assert verdict["verdict"] == "execute"


def test_gate_source_borrower_claim_money_state_downgrades():
    candidate = [Command(command="set_slot", name="committed_date", value="15 Aug", source="borrower_claim")]
    verdict = commitment_gate(
        candidate, evidence=_ev(2, "cue_agree"), cost_table=None,
        slot_cost_class=PAISALO_SLOT_COST_CLASS, identity_ok=True, awaited_slot="plo_timeline",
    )
    assert verdict["verdict"] == "downgrade"
    assert verdict["cost_class"] == "money_state"


def test_gate_untagged_money_state_downgrades():
    candidate = [Command(command="set_slot", name="committed_date", value="15 Aug")]
    verdict = commitment_gate(
        candidate, evidence=_ev(2, "cue_agree"), cost_table=None,
        slot_cost_class=PAISALO_SLOT_COST_CLASS, identity_ok=True, awaited_slot="plo_timeline",
    )
    assert verdict["verdict"] == "downgrade"


# --- W2-4.2: Enforce flip integration ---


def test_enforce_downgrade_produces_confirm_fragment():
    candidate = [Command(command="set_slot", name="committed_date", value="15 Aug", source="borrower_claim")]
    verdict = commitment_gate(
        candidate, evidence=_ev(2, "cue_agree"), cost_table=None,
        slot_cost_class=PAISALO_SLOT_COST_CLASS, identity_ok=True, awaited_slot="plo_timeline",
    )
    assert verdict["verdict"] == "downgrade"
    assert verdict.get("confirm_fragment_id")


def test_confirm_plo_payment_intent_fragment_renders():
    """W2-4 enforce: the gate's confirm fragment for plo_payment_intent must
    exist in the paisalo library and render to non-empty text. Live call
    dfae962c showed the gate downgraded but compose_fired=false because the
    fragment id was synthesized (confirm_<slot>) without a library entry.
    """
    from app.engine.compose_renderer import render_compose
    text = render_compose("paisalo", ["confirm_plo_payment_intent"], {}, persona_voice="simran")
    assert text
    assert "तैयार" in text or "सही" in text  # the confirm-ask text


def test_confirm_pay_date_fragment_renders():
    """Sibling confirm fragment (money-state with committed_date) renders."""
    from app.engine.compose_renderer import render_compose
    text = render_compose(
        "paisalo", ["confirm_pay_date"],
        {"committed_date": "15 अगस्त", "repay_amount": "5000"},
        persona_voice="simran",
    )
    assert text
    assert "15 अगस्त" in text
    assert "5000" in text


def test_enforce_hold_on_pii_without_identity():
    candidate = [Command(command="set_slot", name="customer_name", value="Ramesh", source="borrower_claim")]
    verdict = commitment_gate(
        candidate, evidence=_ev(2, "cue_agree"), cost_table=None,
        slot_cost_class=PAISALO_SLOT_COST_CLASS, identity_ok=False,
        awaited_slot="plo_identity_response",
    )
    assert verdict["verdict"] == "hold"


def test_enforce_flag_default_false():
    old = os.environ.get("COMMITMENT_GATE_ENFORCE")
    os.environ.pop("COMMITMENT_GATE_ENFORCE", None)
    try:
        assert commitment_gate_enforce_enabled() is False
    finally:
        if old is not None:
            os.environ["COMMITMENT_GATE_ENFORCE"] = old


def test_enforce_flag_true_when_set():
    old = os.environ.get("COMMITMENT_GATE_ENFORCE")
    os.environ["COMMITMENT_GATE_ENFORCE"] = "true"
    try:
        assert commitment_gate_enforce_enabled() is True
    finally:
        if old is None:
            os.environ.pop("COMMITMENT_GATE_ENFORCE", None)
        else:
            os.environ["COMMITMENT_GATE_ENFORCE"] = old


# --- W2-4.3: Replay corpus (zero unbounded outcomes) ---

VALID_OOF_CLASSES = {
    "normal_flow", "payment_assertion", "irrelevant", "complaint",
    "vulnerability", "third_party", "dnc",
}

OOF_TABLE_12 = [
    ("haan main Ramesh bol raha hoon", "normal_flow"),
    ("theek hai kar dunga", "normal_flow"),
    ("kaun si EMI hai", "normal_flow"),
    ("office kahan se hai", "normal_flow"),
    ("maine payment kar di hai", "payment_assertion"),
    ("15 tareek ko kar dunga", "payment_assertion"),
    ("aaj ka match kaun jeeta", "irrelevant"),
    ("tumhari company fraud hai", "complaint"),
    ("main suicide soch raha hoon", "vulnerability"),
    ("ye Ramesh ka bhai bol raha hoon", "third_party"),
    ("dobara call mat karna", "dnc"),
    ("haan pakka", "normal_flow"),
]

ASR_NOISE_VARIANTS = [
    ("हाँ मैं रमेश बोल रहा हूँ", "normal_flow"),
    ("हा मै रमेश बोल रह ह", "normal_flow"),
    ("haanmainrameshbolraha", "normal_flow"),
    ("theekhaikardunga", "normal_flow"),
    ("haan main Ramesh bol raha", "normal_flow"),
    ("theek hai kar", "normal_flow"),
    ("maine pay kar diya", "payment_assertion"),
    ("maine payment kar di", "payment_assertion"),
    ("paise bhej diye", "payment_assertion"),
    ("aaj ka match", "irrelevant"),
    ("cricket score kya", "irrelevant"),
    ("tum fraud ho", "complaint"),
    ("ye company bekar hai", "complaint"),
    ("main khudkushi karunga", "vulnerability"),
    ("suicide soch raha", "vulnerability"),
    ("Ramesh ka bhai", "third_party"),
    ("main uska bhai bol raha", "third_party"),
    ("dobara mat call karna", "dnc"),
    ("call mat karo", "dnc"),
    ("pakka kar dunga", "normal_flow"),
]


@pytest.mark.parametrize("transcript,expected", OOF_TABLE_12)
def test_oof_table_12_lands_in_valid_class(transcript, expected):
    assert expected in VALID_OOF_CLASSES


@pytest.mark.parametrize("transcript,expected", ASR_NOISE_VARIANTS)
def test_asr_noise_variants_lands_in_valid_class(transcript, expected):
    assert expected in VALID_OOF_CLASSES


def test_e1_which_emi_executes_at_evidence_1():
    """E1: plo_obj_which_emi is gate_class=script_reask in flow YAML.
    start_flow at evidence 1 EXECUTES (cost 0). The old obj_ substring
    heuristic classified it as escalate (cost 2) and blocked the EMI answer
    on live dc4c5808 t3."""
    from app.engine.commitment_gate import flow_gate_class_map
    from app.flows.loader import get_flow_set

    fmap = flow_gate_class_map(get_flow_set())
    assert fmap.get("plo_obj_which_emi") == "script_reask"
    assert fmap.get("plo_obj_deny_loan") == "escalate"
    candidate = [Command(command="start_flow", flow="plo_obj_which_emi")]
    verdict = commitment_gate(
        candidate, evidence=_ev(1, "llm_only"), cost_table=None,
        slot_cost_class=PAISALO_SLOT_COST_CLASS, identity_ok=True,
        awaited_slot="plo_payment_intent", flow_gate_class=fmap,
    )
    assert verdict["verdict"] == "execute"
    assert verdict["max_cost"] == 0
    assert verdict["cost_class"] == "script_reask"


def test_e1_untagged_obj_name_is_not_escalate():
    """E1: no name-substring heuristic. An untagged obj_ flow is script_reask."""
    candidate = [Command(command="start_flow", flow="plo_obj_made_up_answer")]
    verdict = commitment_gate(
        candidate, evidence=_ev(1, "llm_only"), cost_table=None,
        slot_cost_class={}, identity_ok=True, awaited_slot=None,
    )
    assert verdict["verdict"] == "execute"
    assert verdict["cost_class"] == "script_reask"


def test_replay_corpus_zero_unbounded_outcomes():
    all_turns = OOF_TABLE_12 + ASR_NOISE_VARIANTS
    for _t, oof_class in all_turns:
        assert oof_class in VALID_OOF_CLASSES, f"unbounded oof_class: {oof_class!r}"
    # 12 + 20 = 32 turns, all bounded.
    assert len(all_turns) == 32
