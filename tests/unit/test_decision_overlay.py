"""Sprint 12 — Decision objective-function overlay tests."""

from datetime import UTC, datetime

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import tenant_config
from app.engine.actions import make_action_runner
from app.engine.executor import run
from app.engine.gate import gate
from app.engine.tracker import apply, new_conversation_state
from app.engine.turn import handle_turn
from app.engines_p2.decision_overlay import (
    AGGRESSIVE_PRESSURE_ACTIONS,
    HUMAN_OWNED_ACTIONS,
    OVERLAY_IS_INPUT_NOT_GATE,
    apply_decision_overlay,
    compute_overlay,
    enumerate_candidates,
    extract_signals,
    ptp_max_days_for_trust,
    rank_candidates,
    score_candidate,
)
from app.flows.loader import load_all_flows
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.schemas.command import Command
from app.schemas.decision import DecisionCandidate, DecisionSignals
from app.schemas.state import BorrowerRecord, ConversationState
from tests.fixtures.test_borrowers import B_DUE
from tests.golden.test_executor_helpers import FLOWS, _base_state
from tests.helpers.scripted_clients import ScriptedKB, ScriptedLLM

FLOW_SET = load_all_flows()


def _signals(
    *,
    ability: str = "medium",
    willingness: str = "medium",
    trust: int = 50,
    risk_flags: list[str] | None = None,
) -> DecisionSignals:
    return DecisionSignals(
        trust=trust,
        ability=ability,
        willingness=willingness,
        risk_flags=risk_flags or [],
    )


def _state_with_signals(
    *,
    ability: str = "medium",
    willingness: str = "medium",
    trust: int = 50,
    risk_flags: list[dict] | None = None,
    flow: str = "promise_to_pay",
) -> ConversationState:
    state = _base_state(flow)
    state.slots["trust"] = trust
    state.slots["persona"] = {
        "ability": ability,
        "willingness": willingness,
        "primary_persona": "temporary_hardship",
    }
    if risk_flags is not None:
        state.slots["risk_flags"] = risk_flags
    return state


def _candidate(action_id: str, category: str) -> DecisionCandidate:
    return DecisionCandidate(
        action_id=action_id,
        category=category,
        recovery_value=0.6,
        contact_cost=0.4,
        experience_cost=0.4,
    )


def test_wants_cant_never_selects_aggressive_pressure():
    signals = _signals(ability="low", willingness="high")
    candidates = [
        _candidate("ask_earlier_date", "aggressive_pressure"),
        _candidate("forward_ptp_empathy", "empathy_partial"),
        _candidate("offer_partial_payment", "partial_ptp"),
        _candidate("validate_ptp", "forward_ptp"),
    ]
    ranked = rank_candidates(candidates, signals, "WANTS_CANT")
    ranked_ids = [candidate.action_id for candidate in ranked]

    assert "ask_earlier_date" not in ranked_ids
    assert score_candidate(candidates[0], signals, "WANTS_CANT") == float("-inf")
    assert ranked_ids[0] in ("forward_ptp_empathy", "offer_partial_payment", "validate_ptp")

    state = apply_decision_overlay(_state_with_signals(ability="low", willingness="high"), FLOW_SET)
    assert state.slots["decision_quadrant"] == "WANTS_CANT"
    assert state.slots["pressure_allowed"] is False
    assert "ask_earlier_date" not in state.slots["overlay_ranked_actions"]


def test_can_wont_prefers_firm_factual_not_concessions():
    signals = _signals(ability="high", willingness="low", risk_flags=["strategic_default"])
    candidates = [
        _candidate("offer_partial_payment", "partial_ptp"),
        _candidate("forward_ptp_empathy", "empathy_partial"),
        _candidate("raise_dispute_ticket", "firm_factual"),
        _candidate("behavioral_risk_watch", "firm_factual"),
    ]
    ranked = rank_candidates(candidates, signals, "CAN_WONT")
    ranked_ids = [candidate.action_id for candidate in ranked]

    assert ranked_ids[0] in ("raise_dispute_ticket", "behavioral_risk_watch")
    assert ranked_ids.index("raise_dispute_ticket") < ranked_ids.index("offer_partial_payment")

    state = apply_decision_overlay(
        _state_with_signals(
            ability="high",
            willingness="low",
            risk_flags=[{"flag": "strategic_default", "confidence": 0.9}],
        ),
        FLOW_SET,
    )
    assert state.slots["decision_quadrant"] == "CAN_WONT"
    assert state.slots["decision_strategy"] == "firm_factual"


def test_trust_sets_ptp_window_and_executor_respects_it():
    runner = make_action_runner(FakeToolClient())

    high_trust = _state_with_signals(trust=88)
    high_trust = apply(
        high_trust,
        [Command(command="set_slot", name="ptp_date", value="2026-07-10")],
    )
    high_trust = apply_decision_overlay(high_trust, FLOW_SET)
    assert high_trust.slots["ptp_max_days"] == 21
    high_result = run(high_trust, FLOWS, runner)
    assert high_result.reply_id == "confirm_ptp"

    low_trust = _state_with_signals(trust=30)
    low_trust = apply(
        low_trust,
        [Command(command="set_slot", name="ptp_date", value="2026-07-10")],
    )
    low_trust = apply_decision_overlay(low_trust, FLOW_SET)
    assert low_trust.slots["ptp_max_days"] == 7
    low_result = run(low_trust, FLOWS, runner)
    assert low_result.reply_id == "ask_earlier_date"


def test_ptp_max_days_for_trust_bands():
    assert ptp_max_days_for_trust(88) == 21
    assert ptp_max_days_for_trust(50) == 14
    assert ptp_max_days_for_trust(25) == 7


def test_lambda2_excludes_non_compliant_candidates():
    assert OVERLAY_IS_INPUT_NOT_GATE is True
    signals = _signals(ability="low", willingness="high")
    aggressive = _candidate("ask_earlier_date", "aggressive_pressure")
    assert score_candidate(aggressive, signals, "WANTS_CANT") == float("-inf")


@pytest.mark.compliance
def test_overlay_selected_action_still_passes_gate():
    state = _state_with_signals(ability="high", willingness="low")
    state = apply_decision_overlay(state, FLOW_SET)
    cfg = tenant_config("default")
    now = datetime(2026, 6, 25, 10, 0, tzinfo=UTC)
    result = gate("Police aa jayegi agar payment nahi", state, cfg, now=now)
    assert result.verdict == "modify"
    assert result.level == "CRITICAL"
    assert "police" not in result.text.lower()


def test_human_owned_actions_recommended_not_auto_executed():
    state = _state_with_signals(ability="low", willingness="low")
    state = apply_decision_overlay(state, FLOW_SET)
    overlay = compute_overlay(state, FLOW_SET)

    assert "settlement_review" in overlay.human_recommendations
    assert overlay.selected_action not in HUMAN_OWNED_ACTIONS
    assert overlay.selected_action not in AGGRESSIVE_PRESSURE_ACTIONS

    runner = make_action_runner(FakeToolClient())
    result = run(state, FLOW_SET, runner)
    for action in result.actions_called:
        assert action not in HUMAN_OWNED_ACTIONS


def test_determinism_fixed_signal_inputs():
    state = _state_with_signals(ability="low", willingness="high", trust=58)
    first = compute_overlay(state, FLOW_SET)
    second = compute_overlay(state, FLOW_SET)
    assert first.model_dump() == second.model_dump()


def test_extract_signals_reads_cached_slots_only():
    state = new_conversation_state("c", "default", "b")
    state.slots = {
        "trust": 72,
        "bucket": "30-60",
        "persona": {
            "ability": "high",
            "willingness": "low",
            "primary_persona": "strategic_defaulter",
        },
        "emotion": "anger",
        "emotion_intensity": "high",
        "risk_flags": [{"flag": "strategic_default", "confidence": 0.8}],
    }
    signals = extract_signals(state)
    assert signals.trust == 72
    assert signals.ability == "high"
    assert signals.willingness == "low"
    assert "strategic_default" in signals.risk_flags


def test_enumerate_candidates_from_active_flow_step():
    state = _state_with_signals()
    candidates = enumerate_candidates(state, FLOW_SET)
    ids = {candidate.action_id for candidate in candidates}
    assert "validate_ptp" in ids or "collect:ptp_date" in ids


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
                call_id=f"overlay-lat-{index}",
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
