"""Sprint 13 — Recovery Probability Engine tests."""

from datetime import UTC, datetime

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import tenant_config
from app.engine.gate import gate
from app.engine.tracker import new_conversation_state
from app.engine.turn import handle_turn
from app.engines_p2.decision_overlay import score_candidate
from app.engines_p2.recovery_prob import (
    METHOD_HEURISTIC_V1,
    RECOVERY_IS_INPUT_NOT_LICENSE,
    compute_heuristic_recovery,
    recovery_effort_boost,
    sync_recovery_on_persist,
)
from app.engines_p2.risk import refresh_borrower_risk
from app.engines_p2.trust import refresh_borrower_trust
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.schemas.decision import DecisionCandidate, DecisionSignals
from app.schemas.state import BorrowerRecord
from tests.fixtures.test_borrowers import B_DUE
from tests.fixtures.trust_blueprint_paths import reliable_borrower, slipping_borrower
from tests.helpers.scripted_clients import ScriptedKB, ScriptedLLM


def _prepare(borrower: BorrowerRecord) -> BorrowerRecord:
    ref = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    updated = refresh_borrower_trust(borrower)
    return refresh_borrower_risk(updated, reference=ref)


def _with_signals(
    borrower: BorrowerRecord,
    *,
    bucket: str = "30-60",
    trust: int | None = None,
    persona: str = "genuine_payer",
    risk_flags: list[dict] | None = None,
) -> BorrowerRecord:
    updated = borrower.model_copy(deep=True)
    updated.loan = {**updated.loan, "bucket": bucket, "amount_due": 10000}
    if trust is not None:
        updated.trust_current = trust
    updated.persona_current = {
        "primary_persona": persona,
        "ability": "high" if persona == "genuine_payer" else "low",
        "willingness": "high",
    }
    if risk_flags is not None:
        updated.risk_flags = risk_flags
    return updated


def test_heuristic_monotonicity_trust_and_persona():
    cooperative = compute_heuristic_recovery(
        _with_signals(
            _prepare(reliable_borrower()),
            bucket="0-30",
            trust=88,
            persona="genuine_payer",
        )
    )
    risky = compute_heuristic_recovery(
        _with_signals(
            _prepare(slipping_borrower()),
            bucket="90+",
            trust=30,
            persona="promise_breaker",
            risk_flags=[{"flag": "promise_breaking", "confidence": 0.9}],
        )
    )
    assert cooperative.p_cure > risky.p_cure
    assert cooperative.expected_recovery_pv > risky.expected_recovery_pv


def test_heuristic_higher_bucket_lowers_p_cure():
    current = compute_heuristic_recovery(
        _with_signals(BorrowerRecord(borrower_id="b"), bucket="current")
    )
    delinquent = compute_heuristic_recovery(
        _with_signals(BorrowerRecord(borrower_id="b"), bucket="90+")
    )
    assert current.p_cure > delinquent.p_cure


def test_bounds_p_cure_and_pv():
    score = compute_heuristic_recovery(
        _with_signals(_prepare(slipping_borrower()), bucket="60-90", trust=25)
    )
    assert 0.0 <= score.p_cure <= 1.0
    assert score.expected_recovery_pv >= 0.0
    assert score.method == METHOD_HEURISTIC_V1


def test_method_heuristic_v1_and_ml_path_documented():
    assert RECOVERY_IS_INPUT_NOT_LICENSE is True
    import app.engines_p2.recovery_prob as recovery_module

    doc = recovery_module.__doc__ or ""
    assert "ml_v1" in doc
    assert "cured_within_N_days" in doc
    assert "audit" in doc.lower()


def test_sync_recovery_on_persist_writes_borrower_record():
    borrower = _with_signals(_prepare(reliable_borrower()), bucket="0-30", trust=88)
    updated = sync_recovery_on_persist(borrower)
    assert updated.recovery["method"] == METHOD_HEURISTIC_V1
    assert "p_cure" in updated.recovery
    assert "expected_recovery_pv" in updated.recovery
    assert "last_scored" in updated.recovery


@pytest.mark.compliance
def test_low_p_cure_does_not_relax_gate():
    state = new_conversation_state("c", "default", "b")
    state.slots["recovery"] = {
        "p_cure": 0.05,
        "expected_recovery_pv": 100.0,
        "method": METHOD_HEURISTIC_V1,
    }
    cfg = tenant_config("default")
    now = datetime(2026, 6, 25, 10, 0, tzinfo=UTC)
    result = gate("Police aa jayegi agar payment nahi", state, cfg, now=now)
    assert result.verdict == "modify"
    assert result.level == "CRITICAL"
    assert "police" not in result.text.lower()


def test_determinism_fixed_inputs():
    borrower = _with_signals(_prepare(reliable_borrower()), trust=75, bucket="30-60")
    fixed = datetime(2026, 6, 25, 12, 0, tzinfo=UTC)
    first = compute_heuristic_recovery(borrower, ts=fixed)
    second = compute_heuristic_recovery(borrower, ts=fixed)
    assert first.model_dump() == second.model_dump()


def test_recovery_boost_influences_overlay_scoring():
    signals = DecisionSignals(p_cure=0.8, expected_recovery_pv=50000.0)
    candidate = DecisionCandidate(
        action_id="validate_ptp",
        category="forward_ptp",
        recovery_value=0.75,
        contact_cost=0.35,
        experience_cost=0.25,
    )
    low = score_candidate(candidate, signals, "CAN_WILL", recovery_boost=0.0)
    high = score_candidate(candidate, signals, "CAN_WILL", recovery_boost=0.12)
    assert high > low


def test_recovery_effort_boost_from_slots():
    state = new_conversation_state("c", "default", "b")
    state.slots["recovery"] = {"p_cure": 0.9, "expected_recovery_pv": 25000.0}
    assert recovery_effort_boost(state) > 0.0


@pytest.mark.asyncio
async def test_handle_turn_persists_recovery_in_audit():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:promise_to_pay]] kal"}])
    llm = ScriptedLLM(
        [
            [
                {"command": "start_flow", "flow": "promise_to_pay"},
                {"command": "set_slot", "name": "ptp_date", "value": "2026-06-27"},
            ]
        ]
    )
    await memory.save_borrower(
        BorrowerRecord(
            borrower_id=B_DUE,
            loan={"amount_due": 5000, "dpd": 45, "bucket": "30-60"},
        )
    )
    response = await handle_turn(
        TurnRequest(
            call_id="recovery-audit",
            tenant_id="default",
            borrower_id=B_DUE,
            transcript="kal payment kar dunga",
            turn_meta={"call_date": "2026-06-25"},
        ),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=FakeToolClient(),
    )
    assert response.audit_id
    borrower = await memory.load_borrower(B_DUE)
    assert borrower is not None
    assert borrower.recovery.get("method") == METHOD_HEURISTIC_V1
    assert 0.0 <= float(borrower.recovery.get("p_cure", -1)) <= 1.0


@pytest.mark.asyncio
async def test_handle_turn_does_not_add_second_llm_call():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:promise_to_pay]] kal"}])
    llm = ScriptedLLM(
        [
            [
                {"command": "start_flow", "flow": "promise_to_pay"},
                {"command": "set_slot", "name": "ptp_date", "value": "2026-06-27"},
            ]
        ]
    )
    await memory.save_borrower(
        BorrowerRecord(
            borrower_id=B_DUE,
            loan={"amount_due": 5000, "dpd": 45, "bucket": "30-60"},
        )
    )
    for index in range(5):
        await handle_turn(
            TurnRequest(
                call_id=f"recovery-lat-{index}",
                tenant_id="default",
                borrower_id=B_DUE,
                transcript="kal payment kar dunga",
                turn_meta={"call_date": "2026-06-25"},
            ),
            memory=memory,
            kb=kb,
            llm=llm,
            tools=FakeToolClient(),
        )
    assert llm.call_count == 5
