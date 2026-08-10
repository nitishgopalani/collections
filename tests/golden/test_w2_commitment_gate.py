"""W2-2 Commitment Gate (SHADOW) — pure-function unit tests + shadow replay.

Covers the four spec fixtures:
  - date-vs-amount (money-state, evidence < 3) -> would_downgrade=confirm
  - "theek hai" at intent with cue (evidence 2, neutral-slot cost 1) -> execute
  - "maine pay kar diya" (already_paid claim, money-state cost 3) -> would_downgrade
  - end_call at evidence 1 (cost 2) -> would_downgrade
Plus:
  - cost 0 (script/re-ask) always executes
  - PII without identity_current -> hold
  - non-addressed (evidence 0) with cost > 0 -> hold
  - shadow replay of sessions 0cc56de1 + 660acb01 -> zero behaviour diff,
    verdicts logged sane (gate_verdict present, would_downgrade bool).
"""

from __future__ import annotations

import pytest

from app.engine.commitment_gate import (
    DEFAULT_COST_TABLE,
    commitment_gate,
    commitment_gate_enforce_enabled,
)
from app.engine.evidence_scorer import score_evidence
from app.schemas.command import Command
from app.engine.tracker import new_conversation_state

PAISALO_SLOT_COST_CLASS = {
    "plo_payment_intent": "money_state",
    "plo_timeline": "money_state",
    "plo_identity_response": "pii",
    "customer_name": "pii",
    "repay_amount": "money_state",
    "loan_amount": "money_state",
    "committed_date": "money_state",
}


def _ev(score: int, reason: str = "test") -> dict:
    return {"evidence": score, "evidence_reason": reason, "evidence_signals": {}}


def test_date_vs_amount_money_state_low_evidence_downgrades():
    """Borrower says a date that mismatches the amount on file -> money-state
    set_slot (committed_date, cost 3) with evidence 2 (cue-agree) -> downgrade
    to confirm. The gate must NOT execute a money-state write on cue-only
    evidence."""
    candidate = [Command(command="set_slot", name="committed_date", value="15 Aug")]
    verdict = commitment_gate(
        candidate,
        evidence=_ev(2, "cue_agree"),
        cost_table=None,
        slot_cost_class=PAISALO_SLOT_COST_CLASS,
        identity_ok=True,
        awaited_slot="plo_timeline",
    )
    assert verdict["verdict"] == "downgrade"
    assert verdict["would_downgrade"] is True
    assert verdict["confirm_fragment_id"] == "confirm_committed_date"
    assert verdict["cost_class"] == "money_state"
    assert verdict["max_cost"] == 3


def test_date_vs_amount_money_state_evidence3_executes():
    """Same money-state write but evidence 3 (explicit confirm) -> execute."""
    candidate = [Command(command="set_slot", name="committed_date", value="15 Aug")]
    verdict = commitment_gate(
        candidate,
        evidence=_ev(3, "explicit_confirm"),
        cost_table=None,
        slot_cost_class=PAISALO_SLOT_COST_CLASS,
        identity_ok=True,
        awaited_slot="plo_timeline",
    )
    assert verdict["verdict"] == "execute"
    assert verdict["would_downgrade"] is False


def test_theek_hai_at_neutral_slot_with_cue_evidence2_executes():
    """Neutral collect slot (cost 1) + cue-agree evidence 2 (>= 1) -> execute.
    This is the spec fixture: "theek hai at intent with cue (evidence 2) ->
    execute" for a neutral-slot collect. The willing-commit money-state
    slot is covered by the downgrade branch below."""
    neutral = [Command(command="set_slot", name="some_neutral_slot", value="x")]
    v = commitment_gate(
        neutral,
        evidence=_ev(2, "cue_agree"),
        cost_table=None,
        slot_cost_class=PAISALO_SLOT_COST_CLASS,
        identity_ok=True,
        awaited_slot="some_neutral_slot",
    )
    assert v["verdict"] == "execute"
    assert v["would_downgrade"] is False


def test_theek_hai_at_willing_commit_money_state_downgrades():
    """Willing-commit money-state slot (plo_payment_intent, cost 3) + evidence
    2 (cue-agree) -> downgrade. This is the SHADOW observation signal: the
    gate would downgrade a willing-commit on cue-only evidence. Behaviour
    unchanged in SHADOW (logs only)."""
    willing = [Command(command="set_slot", name="plo_payment_intent", value="willing")]
    v2 = commitment_gate(
        willing,
        evidence=_ev(2, "cue_agree"),
        cost_table=None,
        slot_cost_class=PAISALO_SLOT_COST_CLASS,
        identity_ok=True,
        awaited_slot="plo_payment_intent",
    )
    assert v2["verdict"] == "downgrade"
    assert v2["would_downgrade"] is True
    assert v2["confirm_fragment_id"] == "confirm_plo_payment_intent"


def test_maine_pay_kar_diya_already_paid_claim_downgrades():
    """Borrower claims "maine pay kar diya" (already_paid). The already_paid
    slot is money-state (cost 3). Evidence is cue-agree (2) - the claim
    matches the already_paid cue pack but is NOT an explicit confirm of a
    prior question. Gate downgrades to confirm. In ENFORCE this blocks the
    system-fact slot write and routes the already_paid flow with
    source=borrower_claim; in SHADOW it logs only."""
    candidate = [Command(command="set_slot", name="plo_already_paid", value="true")]
    verdict = commitment_gate(
        candidate,
        evidence=_ev(2, "cue_agree"),
        cost_table=None,
        slot_cost_class=PAISALO_SLOT_COST_CLASS,
        identity_ok=True,
        awaited_slot="plo_already_paid",
    )
    assert verdict["verdict"] == "downgrade"
    assert verdict["would_downgrade"] is True
    assert verdict["cost_class"] == "money_state"
    assert verdict["max_cost"] == 3


def test_end_call_at_evidence1_downgrades():
    """end_call proxy (human_handoff command, cost 2) with evidence 1 (LLM-only)
    -> downgrade. Evidence 1 < 2 -> not enough to commit a call end / handoff.
    (end_call itself is an action-runner decision, not a Command primitive;
    the gate sees the human_handoff command which the action runner turns
    into a transfer/end.)"""
    candidate = [Command(command="human_handoff")]
    verdict = commitment_gate(
        candidate,
        evidence=_ev(1, "llm_only"),
        cost_table=None,
        slot_cost_class=PAISALO_SLOT_COST_CLASS,
        identity_ok=True,
        awaited_slot=None,
    )
    assert verdict["verdict"] == "downgrade"
    assert verdict["would_downgrade"] is True
    assert verdict["cost_class"] == "end_call"
    assert verdict["max_cost"] == 2


def test_end_call_at_evidence2_executes():
    """human_handoff (cost 2) with evidence 2 -> execute."""
    candidate = [Command(command="human_handoff")]
    verdict = commitment_gate(
        candidate,
        evidence=_ev(2, "cue_agree"),
        cost_table=None,
        slot_cost_class=PAISALO_SLOT_COST_CLASS,
        identity_ok=True,
        awaited_slot=None,
    )
    assert verdict["verdict"] == "execute"


def test_script_reask_cost0_always_executes_even_at_evidence0():
    """A start_flow on a script/re-ask flow (cost 0) executes even at
    evidence 0 - the gate never blocks a re-ask."""
    candidate = [Command(command="start_flow", flow="plo_predue_greeting")]
    verdict = commitment_gate(
        candidate,
        evidence=_ev(0, "non_addressed"),
        cost_table=None,
        slot_cost_class=PAISALO_SLOT_COST_CLASS,
        identity_ok=True,
        awaited_slot=None,
    )
    assert verdict["verdict"] == "execute"
    assert verdict["max_cost"] == 0


def test_clarify_cost0_executes():
    """A clarify command (cost 0) always executes - clarification is a re-ask."""
    candidate = [Command(command="clarify", slot="plo_payment_intent")]
    verdict = commitment_gate(
        candidate,
        evidence=_ev(0, "non_addressed"),
        cost_table=None,
        slot_cost_class=PAISALO_SLOT_COST_CLASS,
        identity_ok=True,
        awaited_slot="plo_payment_intent",
    )
    assert verdict["verdict"] == "execute"


def test_pii_slot_without_identity_current_holds():
    """A set_slot on a PII slot (customer_name) without identity_current
    (identity_ok=False) -> hold (disclosure locked). Even at evidence 3."""
    candidate = [Command(command="set_slot", name="customer_name", value="Ramesh")]
    verdict = commitment_gate(
        candidate,
        evidence=_ev(3, "explicit_confirm"),
        cost_table=None,
        slot_cost_class=PAISALO_SLOT_COST_CLASS,
        identity_ok=False,
        awaited_slot="customer_name",
    )
    assert verdict["verdict"] == "hold"
    assert verdict["reason"] == "pii_without_identity_current"
    assert verdict["would_downgrade"] is False


def test_pii_slot_with_identity_current_executes_at_evidence3():
    """Same PII slot with identity_current=True + evidence 3 -> execute."""
    candidate = [Command(command="set_slot", name="customer_name", value="Ramesh")]
    verdict = commitment_gate(
        candidate,
        evidence=_ev(3, "explicit_confirm"),
        cost_table=None,
        slot_cost_class=PAISALO_SLOT_COST_CLASS,
        identity_ok=True,
        awaited_slot="customer_name",
    )
    assert verdict["verdict"] == "execute"


def test_non_addressed_evidence0_with_cost_holds():
    """evidence 0 (non-addressed) + any cost > 0 -> hold. Confirming a
    non-addressed turn is pointless; the gate holds (re-ask)."""
    candidate = [Command(command="set_slot", name="plo_payment_intent", value="willing")]
    verdict = commitment_gate(
        candidate,
        evidence=_ev(0, "non_addressed"),
        cost_table=None,
        slot_cost_class=PAISALO_SLOT_COST_CLASS,
        identity_ok=True,
        awaited_slot="plo_payment_intent",
    )
    assert verdict["verdict"] == "hold"
    assert verdict["reason"] == "non_addressed"


def test_default_cost_table_values_match_spec():
    assert DEFAULT_COST_TABLE == {
        "script_reask": 0,
        "speak_fact": 1,
        "neutral_slot": 1,
        "escalate": 2,
        "end_call": 2,
        "money_state": 3,
        "pii": 3,
    }


def test_tenant_cost_table_override():
    """A tenant can override a cost row (e.g. bump escalate to 3)."""
    candidate = [Command(command="start_flow", flow="plo_obj_handoff")]
    verdict = commitment_gate(
        candidate,
        evidence=_ev(2, "cue_agree"),
        cost_table={"escalate": 3},
        slot_cost_class=PAISALO_SLOT_COST_CLASS,
        identity_ok=True,
        awaited_slot=None,
    )
    assert verdict["verdict"] == "downgrade"
    assert verdict["max_cost"] == 3


def test_enforce_flag_defaults_false(monkeypatch):
    monkeypatch.delenv("COMMITMENT_GATE_ENFORCE", raising=False)
    assert commitment_gate_enforce_enabled() is False


def test_enforce_flag_true_when_set(monkeypatch):
    monkeypatch.setenv("COMMITMENT_GATE_ENFORCE", "true")
    assert commitment_gate_enforce_enabled() is True


def test_mixed_candidate_highest_cost_wins():
    """A candidate with a neutral-slot set_slot (cost 1) AND an end_call
    (cost 2) -> the gate uses the highest cost (2). Evidence 1 < 2 ->
    downgrade. confirm_fragment_id points at the highest-cost slot."""
    candidate = [
        Command(command="set_slot", name="some_neutral_slot", value="x"),
        Command(command="human_handoff"),
    ]
    verdict = commitment_gate(
        candidate,
        evidence=_ev(1, "llm_only"),
        cost_table=None,
        slot_cost_class=PAISALO_SLOT_COST_CLASS,
        identity_ok=True,
        awaited_slot="some_neutral_slot",
    )
    assert verdict["verdict"] == "downgrade"
    assert verdict["max_cost"] == 2
    assert verdict["cost_class"] == "end_call"
    assert verdict["confirm_fragment_id"] == "confirm_some_neutral_slot"


# ---------------------------------------------------------------------------
# Shadow replay: sessions 0cc56de1 + 660acb01 -> zero behaviour diff
# Each line is fed through score_evidence + commitment_gate as a pure-function
# shadow; the assertion is that the verdict is sane (one of execute/downgrade/
# hold) and would_downgrade is a bool. Behaviour diff is zero by construction
# (the gate is pure and not wired into the commit path in SHADOW).
# ---------------------------------------------------------------------------

SESSION_0CC56DE1 = [
    ("haan main Ramesh bol raha hoon", "plo_identity_response"),
    ("theek hai kar dunga", "plo_payment_intent"),
    ("accha suno main Ramesh ka bhai bol raha hoon wo bahar gaya hai", "plo_identity_response"),
]

SESSION_660ACB01 = [
    ("haan ji", "plo_identity_response"),
    ("kal tak kar dunga", "plo_timeline"),
    ("nahi abhi nahi kar sakta", "plo_payment_intent"),
]


@pytest.mark.parametrize("transcript,awaited_slot", SESSION_0CC56DE1 + SESSION_660ACB01)
def test_shadow_replay_sessions_zero_behaviour_diff(transcript, awaited_slot, monkeypatch):
    """Shadow replay: each session line -> evidence score + gate verdict
    (sane class). Zero behaviour diff by construction (gate is pure, not
    wired into commit in SHADOW). Enforce flag off."""
    monkeypatch.delenv("COMMITMENT_GATE_ENFORCE", raising=False)
    state = new_conversation_state("call-shadow", "paisalo", "borrower-shadow")
    state.slots["last_spoken_reply"] = "namaste main Priya bol rahi hoon PaisaLo se."
    state.slots["_last_borrower_transcript"] = ""
    state.slots["identity_ok"] = True

    ev = score_evidence(
        transcript=transcript,
        state=state,
        profile=None,
        llm_calls=1,
        commands=[Command(command="set_slot", name=awaited_slot, value="x")],
        last_spoken_reply=state.slots["last_spoken_reply"],
        echo=False,
        awaited_slot=awaited_slot,
    )
    verdict = commitment_gate(
        [Command(command="set_slot", name=awaited_slot, value="x")],
        evidence=ev,
        cost_table=None,
        slot_cost_class=PAISALO_SLOT_COST_CLASS,
        identity_ok=True,
        awaited_slot=awaited_slot,
    )
    assert verdict["verdict"] in ("execute", "downgrade", "hold")
    assert isinstance(verdict["would_downgrade"], bool)
    assert isinstance(verdict["confirm_fragment_id"], (str, type(None)))
    if "identity" in awaited_slot.lower():
        assert verdict["cost_class"] == "pii"
    elif any(m in awaited_slot.lower() for m in ("payment_intent", "timeline")):
        assert verdict["cost_class"] == "money_state"
