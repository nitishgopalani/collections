"""FS-3 hardship shared flow tests."""

from datetime import UTC, datetime

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import tenant_config
from app.engine.hardship import (
    has_corroborated_hardship_with_partials,
    reply_has_pressure_language,
)
from app.engine.retrieval import clear_retrieval_cache
from app.engine.safety import safety_preempt
from app.engine.tracker import apply, new_conversation_state
from app.engine.turn import handle_turn
from app.engines_p2.decision_overlay import (
    AGGRESSIVE_PRESSURE_ACTIONS,
    apply_decision_overlay,
    compute_overlay,
    enumerate_candidates,
    rank_candidates,
    score_candidate,
)
from app.engines_p2.persona import classify_persona_rules
from app.engines_p2.risk import compute_risk_flags
from app.flows.loader import load_all_flows
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.schemas.command import Command
from app.schemas.state import BorrowerRecord
from tests.fixtures.test_borrowers import B_DUE
from tests.helpers.scripted_clients import ScriptedKB, ScriptedLLM

FLOWS = load_all_flows()
_REF = datetime(2026, 6, 25, 12, 0, tzinfo=UTC)

HARDSHIP_REASONS = [
    "job_loss",
    "medical",
    "business",
    "reduced_income",
    "competing_obligations",
]


def _verified_borrower(borrower_id: str = B_DUE) -> BorrowerRecord:
    return BorrowerRecord(
        borrower_id=borrower_id,
        loan={"amount_due": 5000, "dpd": 45, "bucket": "30-60"},
        identity={"identity_ok": True},
    )


def _turn(call_id: str, borrower_id: str, transcript: str) -> TurnRequest:
    return TurnRequest(
        call_id=call_id,
        tenant_id="default",
        borrower_id=borrower_id,
        transcript=transcript,
        turn_meta={"call_date": "2026-06-25"},
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_retrieval_cache()
    yield
    clear_retrieval_cache()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", HARDSHIP_REASONS)
async def test_each_hardship_reason_empathy_first_no_pressure(reason):
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": f"[[flow:hardship]] {reason}"}])
    llm = ScriptedLLM(
        [
            [
                {"command": "start_flow", "flow": "hardship"},
                {"command": "set_slot", "name": "hardship_reason", "value": reason},
            ],
            [{"command": "set_slot", "name": "hardship_path", "value": "review"}],
        ]
    )
    tools = FakeToolClient()
    tools.reset()
    await memory.save_borrower(_verified_borrower())

    first = await handle_turn(
        _turn(f"call-hs-{reason}", B_DUE, f"hardship {reason}"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )

    assert "apply_hardship_empathy" in first.actions_executed
    assert "[COMPLIANCE-REVIEW]" in first.reply_text
    assert not reply_has_pressure_language(first.reply_text)

    second = await handle_turn(
        _turn(f"call-hs-{reason}", B_DUE, "hardship review chahiye"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert "set_hardship_disposition" in second.actions_executed
    assert second.disposition == "FORBEARANCE_REVIEW"
    assert not reply_has_pressure_language(second.reply_text)


def test_corroborated_hardship_with_partials_blocks_excuse_recycling():
    borrower = BorrowerRecord(
        borrower_id="B_CORROB",
        hardships=[
            {
                "type": "job_loss",
                "status": "corroborated",
                "onset": "2026-06-01",
                "ts": "2026-06-01T10:00:00+00:00",
            }
        ],
        payments=[
            {"date": "2026-06-10T10:00:00+00:00", "partial": True, "amount": 2000},
        ],
        excuses=[
            {"text": "salary delay", "date": "2026-01-05T10:00:00+00:00"},
            {"text": "salary delay", "date": "2026-01-15T10:00:00+00:00"},
            {"text": "salary delay", "date": "2026-01-25T10:00:00+00:00"},
        ],
    )
    assert has_corroborated_hardship_with_partials(borrower)
    flags = compute_risk_flags(borrower, reference=_REF)
    flag_names = {f["flag"] for f in flags}
    assert "excuse_recycling" not in flag_names

    persona = classify_persona_rules(borrower)
    assert persona.primary_persona == "temporary_hardship"


def test_wants_cant_overlay_never_selects_aggressive_pressure():
    state = new_conversation_state("c-overlay", "default", "b")
    state.slots["identity_ok"] = True
    state.slots["hardship_active"] = True
    state.slots["persona"] = {
        "ability": "low",
        "willingness": "high",
        "primary_persona": "temporary_hardship",
    }
    state = apply(state, [Command(command="start_flow", flow="hardship")])

    state = apply_decision_overlay(state, FLOWS)
    overlay = compute_overlay(state, FLOWS)
    assert overlay.quadrant == "WANTS_CANT"
    assert overlay.pressure_allowed is False

    candidates = enumerate_candidates(state, FLOWS)
    from app.engines_p2.decision_overlay import extract_signals

    signals_obj = extract_signals(state)
    signals_obj = signals_obj.model_copy(update={"ability": "low", "willingness": "high"})
    ranked = rank_candidates(candidates, signals_obj, "WANTS_CANT")
    ranked_ids = {c.action_id for c in ranked}
    assert not (ranked_ids & AGGRESSIVE_PRESSURE_ACTIONS)
    for action in AGGRESSIVE_PRESSURE_ACTIONS:
        cand = next((c for c in candidates if c.action_id == action), None)
        if cand:
            assert score_candidate(cand, signals_obj, "WANTS_CANT") == float("-inf")


@pytest.mark.asyncio
async def test_hardship_history_written_for_risk_and_persona():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:hardship]] medical"}])
    llm = ScriptedLLM(
        [
            [
                {"command": "start_flow", "flow": "hardship"},
                {"command": "set_slot", "name": "hardship_reason", "value": "medical"},
            ],
            [{"command": "set_slot", "name": "hardship_path", "value": "partial"}],
        ]
    )
    tools = FakeToolClient()
    tools.reset()
    await memory.save_borrower(_verified_borrower())

    await handle_turn(
        _turn("call-hs-persist", B_DUE, "medical bills bahut hain"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    await handle_turn(
        _turn("call-hs-persist", B_DUE, "partial payment"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )

    borrower = await memory.load_borrower(B_DUE)
    assert borrower is not None
    assert len(borrower.hardships) >= 1
    assert borrower.hardships[-1]["type"] == "medical"
    assert borrower.hardships[-1]["source"] == "hardship_flow"


@pytest.mark.asyncio
async def test_vague_ptp_routes_to_hardship_when_context():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:vague_ptp]] soon"}])
    llm = ScriptedLLM(
        [
            [
                {"command": "start_flow", "flow": "vague_ptp"},
                {"command": "set_slot", "name": "hardship_reason", "value": "job_loss"},
            ],
        ]
    )
    tools = FakeToolClient()
    tools.reset()
    await memory.save_borrower(_verified_borrower())

    response = await handle_turn(
        _turn("call-vague-hs", B_DUE, "jald kar dunga kabhi"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )

    assert "mark_vague_ptp" in response.actions_executed
    assert "route_to_hardship" in response.actions_executed
    assert response.disposition != "PAYMENT_CONFIRMED"
    assert not reply_has_pressure_language(response.reply_text)

    state = await memory.load_state("call-vague-hs")
    assert state is not None
    assert any(frame.flow == "hardship" for frame in state.flow_stack)


@pytest.mark.asyncio
async def test_vague_ptp_tightens_to_specific_ptp_without_hardship():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:vague_ptp]] soon"}])
    llm = ScriptedLLM([[{"command": "start_flow", "flow": "vague_ptp"}]])
    tools = FakeToolClient()
    tools.reset()
    await memory.save_borrower(_verified_borrower())

    response = await handle_turn(
        _turn("call-vague-ptp", B_DUE, "jald hi kar dunga"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )

    assert "push_specify_ptp" in response.actions_executed
    assert "route_to_hardship" not in response.actions_executed
    state = await memory.load_state("call-vague-ptp")
    assert state is not None
    assert any(frame.flow == "promise_to_pay" for frame in state.flow_stack)


def test_distress_within_hardship_still_safety_preempts():
    state = new_conversation_state("c-safe", "default", "b")
    state = apply(state, [Command(command="start_flow", flow="hardship")])
    state.slots["hardship_active"] = True
    cfg = tenant_config("default")
    result = safety_preempt("Main suicide soch raha hoon", state, cfg)
    assert result is not None
    assert result.transfer_to_human is True


def test_hardship_history_visible_to_persona_with_partials():
    borrower = BorrowerRecord(
        borrower_id="B_HS_PERSONA",
        hardships=[{"type": "medical", "status": "corroborated", "onset": "2026-06-01"}],
        payments=[{"date": "2026-06-10", "partial": True, "amount": 1500}],
        trust_current=55,
        ptps=[{"promised_date": "2026-06-15", "status": "kept", "paid_on": "2026-06-15"}],
    )
    persona = classify_persona_rules(borrower)
    assert persona.primary_persona == "temporary_hardship"


def test_hardship_beats_ptp_on_priority_ladder():
    from app.engine.priority import reorder

    state = new_conversation_state("c-hs-pri", "default", "b")
    state = apply(
        state,
        [
            Command(command="start_flow", flow="promise_to_pay"),
            Command(command="start_flow", flow="hardship"),
        ],
    )
    reorder(state, FLOWS)
    assert state.flow_stack[-1].flow == "hardship"
    assert state.flow_stack[0].parked is True
