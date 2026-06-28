"""Sprint 9 — Behavioral Risk Engine tests."""

from datetime import UTC, datetime

import pytest

from app.config import tenant_config
from app.engine.gate import gate
from app.engine.tracker import hydrate_from_borrower, new_conversation_state
from app.engines_p2.risk import (
    FAIRNESS_BEHAVIOR_ONLY,
    RISK_IS_INPUT_NOT_LICENSE,
    apply_risk_to_state,
    compute_risk_flags,
    detect_risk_flags,
    refresh_borrower_risk,
    sync_risk_on_persist,
)
from app.schemas.state import BorrowerRecord

_REF = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def _flag_names(flags: list[dict]) -> set[str]:
    return {str(f["flag"]) for f in flags}


def _flag_confidence(flags: list[dict], name: str) -> float:
    for flag in flags:
        if flag["flag"] == name:
            return float(flag["confidence"])
    return 0.0


def test_excuse_recycling_fires_on_third_identical_excuse_no_payment():
    borrower = BorrowerRecord(
        borrower_id="B_RECYCLE",
        excuses=[
            {"text": "salary delay", "date": "2026-01-05T10:00:00+00:00"},
            {"text": "salary late", "date": "2026-01-15T10:00:00+00:00"},
            {"text": "salary nahi aayi", "date": "2026-01-25T10:00:00+00:00"},
        ],
    )
    flags = compute_risk_flags(borrower, reference=_REF)
    assert "excuse_recycling" in _flag_names(flags)


def test_excuse_recycling_not_when_partials_corroborate():
    borrower = BorrowerRecord(
        borrower_id="B_HARDSHIP",
        excuses=[
            {"text": "salary delay", "date": "2026-01-05T10:00:00+00:00"},
            {"text": "salary delay", "date": "2026-01-15T10:00:00+00:00"},
            {"text": "salary delay", "date": "2026-01-25T10:00:00+00:00"},
        ],
        payments=[
            {"date": "2026-01-10T10:00:00+00:00", "partial": True, "amount": 2000},
            {"date": "2026-01-20T10:00:00+00:00", "partial": True, "amount": 1500},
        ],
    )
    flags = compute_risk_flags(borrower, reference=_REF)
    assert "excuse_recycling" not in _flag_names(flags)


def test_ghosting_relative_to_borrower_baseline():
    responsive = BorrowerRecord(
        borrower_id="B_RESPONSIVE",
        excuses=[
            {"text": "will pay", "date": "2026-01-01T10:00:00+00:00"},
            {"text": "ok", "date": "2026-01-04T10:00:00+00:00"},
            {"text": "sure", "date": "2026-01-07T10:00:00+00:00"},
            {"text": "yes", "date": "2026-01-10T10:00:00+00:00"},
        ],
        ptps=[{"promised_date": "2026-01-20T10:00:00+00:00", "status": "pending"}],
    )
    slow = BorrowerRecord(
        borrower_id="B_SLOW",
        excuses=[
            {"text": "will pay", "date": "2026-01-01T10:00:00+00:00"},
            {"text": "ok", "date": "2026-01-15T10:00:00+00:00"},
            {"text": "sure", "date": "2026-01-29T10:00:00+00:00"},
        ],
        ptps=[{"promised_date": "2026-02-05T10:00:00+00:00", "status": "pending"}],
    )
    ref = datetime(2026, 2, 20, 12, 0, tzinfo=UTC)
    responsive_flags = compute_risk_flags(responsive, reference=ref)
    slow_flags = compute_risk_flags(slow, reference=ref)
    assert "ghosting" in _flag_names(responsive_flags)
    assert "ghosting" not in _flag_names(slow_flags)


def test_strategic_default_requires_multiple_signals():
    single_signal = BorrowerRecord(
        borrower_id="B_DISMISS",
        notes=[{"text": "won't pay, do whatever you want"}],
    )
    multi_signal = BorrowerRecord(
        borrower_id="B_STRATEGIC",
        payments=[{"date": "2025-12-01T10:00:00+00:00", "full": True, "amount": 5000}],
        broken_ptps=[
            {"promised_date": "2026-01-10", "broken_on": "2026-01-12"},
            {"promised_date": "2026-01-20", "broken_on": "2026-01-23"},
        ],
        ptps=[
            {"promised_date": "2026-01-10", "status": "broken"},
            {"promised_date": "2026-01-20", "status": "broken"},
        ],
        notes=[{"text": "won't pay, time pass"}],
    )
    assert "strategic_default" not in _flag_names(compute_risk_flags(single_signal, reference=_REF))
    assert "strategic_default" in _flag_names(compute_risk_flags(multi_signal, reference=_REF))


def test_fraud_label_requires_multiple_signals():
    single = BorrowerRecord(
        borrower_id="B_FRAUD1",
        notes=[{"text": "this is identity theft, not my loan"}],
    )
    corroborated = BorrowerRecord(
        borrower_id="B_FRAUD2",
        notes=[{"text": "identity theft, someone else took this loan"}],
        identity={"kyc_mismatch": True},
    )
    assert "fraud_indicator" not in _flag_names(compute_risk_flags(single, reference=_REF))
    assert "fraud_indicator" in _flag_names(compute_risk_flags(corroborated, reference=_REF))


def test_flag_decays_after_improved_behavior():
    borrower = BorrowerRecord(
        borrower_id="B_DECAY",
        excuses=[
            {"text": "salary delay", "date": "2026-01-05T10:00:00+00:00"},
            {"text": "salary delay", "date": "2026-01-15T10:00:00+00:00"},
            {"text": "salary delay", "date": "2026-01-25T10:00:00+00:00"},
        ],
    )
    initial = compute_risk_flags(borrower, reference=_REF)
    assert "excuse_recycling" in _flag_names(initial)

    borrower.risk_flags = initial
    borrower.ptps.append(
        {
            "promised_date": "2026-02-10T10:00:00+00:00",
            "status": "kept",
            "paid_on": "2026-02-10T10:00:00+00:00",
        }
    )
    borrower.payments.append({"date": "2026-02-10T10:00:00+00:00", "full": True, "amount": 5000})
    ref_after = datetime(2026, 2, 15, 12, 0, tzinfo=UTC)
    after = compute_risk_flags(borrower, reference=ref_after)
    assert "excuse_recycling" not in _flag_names(after)
    assert _flag_confidence(after, "excuse_recycling") == 0.0


def test_fairness_identical_behavior_different_identity():
    behavior = {
        "excuses": [
            {"text": "salary delay", "date": "2026-01-05T10:00:00+00:00"},
            {"text": "salary delay", "date": "2026-01-15T10:00:00+00:00"},
            {"text": "salary delay", "date": "2026-01-25T10:00:00+00:00"},
        ],
        "broken_ptps": [
            {"promised_date": "2026-01-10", "broken_on": "2026-01-12"},
            {"promised_date": "2026-01-20", "broken_on": "2026-01-23"},
        ],
        "ptps": [
            {"promised_date": "2026-01-10", "status": "broken"},
            {"promised_date": "2026-01-20", "status": "broken"},
        ],
    }
    north = BorrowerRecord(
        borrower_id="B_NORTH",
        identity={"state": "Delhi", "language": "hi", "region": "north"},
        loan={"source": "branch_a"},
        **behavior,
    )
    south = BorrowerRecord(
        borrower_id="B_SOUTH",
        identity={"state": "Chennai", "language": "ta", "region": "south"},
        loan={"source": "branch_z"},
        **behavior,
    )
    flags_a = compute_risk_flags(north, reference=_REF)
    flags_b = compute_risk_flags(south, reference=_REF)
    assert _flag_names(flags_a) == _flag_names(flags_b)
    assert [f["confidence"] for f in flags_a] == [f["confidence"] for f in flags_b]


def test_fairness_guardrail_constant():
    assert FAIRNESS_BEHAVIOR_ONLY is True
    assert RISK_IS_INPUT_NOT_LICENSE is True


@pytest.mark.compliance
def test_high_risk_borrower_gate_still_blocks_threats():
    state = new_conversation_state("c", "default", "b")
    state.slots["risk_flags"] = [
        {"flag": "strategic_default", "confidence": 0.95, "reason": "test"},
        {"flag": "fraud_indicator", "confidence": 0.9, "reason": "test"},
    ]
    state.slots["compliance_flags"] = {}
    cfg = tenant_config("default")
    now = datetime(2026, 6, 25, 10, 0, tzinfo=UTC)
    result = gate("Police aa jayegi agar payment nahi", state, cfg, now=now)
    assert result.verdict == "modify"
    assert result.level == "CRITICAL"
    assert "police" not in result.text.lower()


@pytest.mark.compliance
def test_high_risk_borrower_gate_blocks_third_party_disclosure():
    state = new_conversation_state("c", "default", "b")
    state.slots["risk_flags"] = [
        {"flag": "strategic_default", "confidence": 0.99, "reason": "test"},
    ]
    cfg = tenant_config("default")
    now = datetime(2026, 6, 25, 10, 0, tzinfo=UTC)
    result = gate("Main wife ko bata dunga", state, cfg, now=now)
    assert result.verdict == "modify"
    assert "wife" not in result.text.lower()


def test_hydrate_exposes_risk_flags_slot():
    borrower = refresh_borrower_risk(
        BorrowerRecord(
            borrower_id="B_SLOT",
            excuses=[
                {"text": "salary delay", "date": "2026-01-05T10:00:00+00:00"},
                {"text": "salary delay", "date": "2026-01-15T10:00:00+00:00"},
                {"text": "salary delay", "date": "2026-01-25T10:00:00+00:00"},
            ],
        ),
        reference=_REF,
    )
    state = new_conversation_state("c", "default", borrower.borrower_id)
    hydrated = hydrate_from_borrower(state, borrower)
    assert "excuse_recycling" in _flag_names(hydrated.slots["risk_flags"])


def test_sync_risk_on_persist():
    borrower = sync_risk_on_persist(
        BorrowerRecord(
            borrower_id="B_SYNC",
            broken_ptps=[
                {"promised_date": "2026-01-10", "broken_on": "2026-01-12"},
                {"promised_date": "2026-01-20", "broken_on": "2026-01-23"},
            ],
            ptps=[
                {"promised_date": "2026-01-10", "status": "broken"},
                {"promised_date": "2026-01-20", "status": "broken"},
            ],
        ),
        reference=_REF,
    )
    assert "promise_breaking" in _flag_names(borrower.risk_flags)


def test_apply_risk_to_state():
    borrower = BorrowerRecord(
        borrower_id="B_APPLY",
        risk_flags=[{"flag": "ghosting", "confidence": 0.7, "reason": "test", "evidence": []}],
    )
    state = new_conversation_state("c", "default", "b")
    updated = apply_risk_to_state(state, borrower)
    assert updated.slots["risk_flags"][0]["flag"] == "ghosting"


def test_promise_breaking_pattern_not_single_event():
    one_break = BorrowerRecord(
        borrower_id="B_ONE",
        broken_ptps=[{"promised_date": "2026-01-10", "broken_on": "2026-01-12"}],
        ptps=[{"promised_date": "2026-01-10", "status": "broken"}],
    )
    assert "promise_breaking" not in _flag_names(compute_risk_flags(one_break, reference=_REF))

    two_breaks = BorrowerRecord(
        borrower_id="B_TWO",
        broken_ptps=[
            {"promised_date": "2026-01-10", "broken_on": "2026-01-12"},
            {"promised_date": "2026-01-18", "broken_on": "2026-01-20"},
        ],
        ptps=[
            {"promised_date": "2026-01-10", "status": "broken"},
            {"promised_date": "2026-01-18", "status": "broken"},
        ],
    )
    assert "promise_breaking" in _flag_names(compute_risk_flags(two_breaks, reference=_REF))


def test_detect_risk_flags_explainable():
    borrower = BorrowerRecord(
        borrower_id="B_EXPLAIN",
        excuses=[
            {"text": "salary delay", "date": "2026-01-05T10:00:00+00:00"},
            {"text": "salary delay", "date": "2026-01-15T10:00:00+00:00"},
            {"text": "salary delay", "date": "2026-01-25T10:00:00+00:00"},
        ],
    )
    result = detect_risk_flags(borrower, reference=_REF)
    flag = next(f for f in result.flags if f.flag == "excuse_recycling")
    assert flag.reason
    assert flag.evidence
