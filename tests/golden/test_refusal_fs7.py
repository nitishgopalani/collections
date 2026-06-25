"""FS-7 refusal & negotiation→human flow tests."""

from datetime import UTC, datetime

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import tenant_config
from app.engine.gate import gate
from app.engine.hardship import reply_has_pressure_language
from app.engine.refusal_negotiation import (
    has_genuine_hardship_context,
    reply_has_threat_or_false_urgency,
    reply_quotes_unauthorized_terms,
)
from app.engine.retrieval import clear_retrieval_cache
from app.engine.tracker import apply, new_conversation_state
from app.engine.turn import handle_turn
from app.engines_p2.decision_overlay import (
    AGGRESSIVE_PRESSURE_ACTIONS,
    HUMAN_OWNED_ACTIONS,
    apply_decision_overlay,
    compute_overlay,
    enumerate_candidates,
    extract_signals,
    rank_candidates,
    score_candidate,
)
from app.flows.loader import load_all_flows
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.schemas.command import Command
from app.schemas.state import BorrowerRecord
from tests.fixtures.test_borrowers import B_DUE
from tests.helpers.scripted_clients import ScriptedKB, ScriptedLLM

FLOWS = load_all_flows()
_REF = datetime(2026, 6, 25, 10, 0, tzinfo=UTC)


def _verified_borrower(**extra) -> BorrowerRecord:
    base = {
        "borrower_id": B_DUE,
        "loan": {"amount_due": 5000, "dpd": 45, "bucket": "30-60"},
        "identity": {"identity_ok": True},
    }
    base.update(extra)
    return BorrowerRecord(**base)


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
async def test_direct_refusal_factual_no_threat_documented():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:direct_refusal]]"}])
    llm = ScriptedLLM([[{"command": "start_flow", "flow": "direct_refusal"}]])
    tools = FakeToolClient()
    await memory.save_borrower(_verified_borrower())

    response = await handle_turn(
        _turn("call-refusal", B_DUE, "main nahi dunga"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )

    assert "apply_firm_factual_refusal" in response.actions_executed
    assert "document_refusal" in response.actions_executed
    assert response.disposition == "REFUSAL"
    assert not reply_has_threat_or_false_urgency(response.reply_text)
    assert not reply_has_pressure_language(response.reply_text)
    cfg = tenant_config("default")
    gate_result = gate(response.reply_text, await memory.load_state("call-refusal"), cfg, now=_REF)
    assert gate_result.verdict != "block"
    assert "police" not in response.reply_text.lower()

    borrower = await memory.load_borrower(B_DUE)
    assert any(note.get("type") == "refusal" for note in borrower.notes)


@pytest.mark.asyncio
async def test_court_acknowledgment_calm_documented():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:court_acknowledgment]]"}])
    llm = ScriptedLLM([[{"command": "start_flow", "flow": "court_acknowledgment"}]])
    tools = FakeToolClient()
    await memory.save_borrower(_verified_borrower())

    response = await handle_turn(
        _turn("call-court", B_DUE, "court le jao mujhe"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )

    assert "document_refusal" in response.actions_executed
    assert response.disposition == "REFUSAL"
    assert not reply_has_threat_or_false_urgency(response.reply_text)
    assert not reply_has_pressure_language(response.reply_text)


@pytest.mark.asyncio
async def test_strategic_default_watch_composes_can_wont_no_concessions():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:strategic_default_watch]]"}])
    llm = ScriptedLLM([[{"command": "start_flow", "flow": "strategic_default_watch"}]])
    tools = FakeToolClient()
    await memory.save_borrower(
        _verified_borrower(
            risk_flags=[{"flag": "strategic_default", "confidence": 0.9, "reason": "test"}],
        )
    )

    response = await handle_turn(
        _turn("call-strat", B_DUE, "jaan bujh kar nahi dunga"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )

    assert "apply_strategic_default_watch" in response.actions_executed
    assert response.disposition == "STRATEGIC_DEFAULT_WATCH"
    assert not reply_has_pressure_language(response.reply_text)
    assert not reply_has_threat_or_false_urgency(response.reply_text)
    assert "waive" not in response.reply_text.lower()

    state = await memory.load_state("call-strat")
    assert state.slots.get("pressure_allowed") is False
    assert state.slots.get("concessions_allowed") is False
    assert state.slots.get("strategic_default_watch") is True
    state.slots["persona"] = {
        "ability": "high",
        "willingness": "low",
        "primary_persona": "strategic_defaulter",
    }
    state = apply_decision_overlay(state, FLOWS)
    overlay = compute_overlay(state, FLOWS)
    assert overlay.quadrant == "CAN_WONT"
    candidates = enumerate_candidates(state, FLOWS)
    signals = extract_signals(state)
    ranked = rank_candidates(candidates, signals, "CAN_WONT")
    for action in AGGRESSIVE_PRESSURE_ACTIONS:
        cand = next((c for c in candidates if c.action_id == action), None)
        if cand:
            assert score_candidate(cand, signals, "CAN_WONT") == float("-inf")
    assert not {c.action_id for c in ranked} & AGGRESSIVE_PRESSURE_ACTIONS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flow", "disposition", "prepare_action"),
    [
        ("settlement_review", "SETTLEMENT_REVIEW", "prepare_settlement_review"),
        ("restructure_review", "RESTRUCTURE_REVIEW", "prepare_restructure_review"),
        ("moratorium_review", "MORATORIUM_REVIEW", "prepare_moratorium_review"),
    ],
)
async def test_negotiation_routed_human_no_unauthorized_terms(
    flow, disposition, prepare_action
):
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": f"[[flow:{flow}]]"}])
    llm = ScriptedLLM(
        [
            [{"command": "start_flow", "flow": flow}],
            [{"command": "set_slot", "name": "negotiation_request", "value": "need help"}],
        ]
    )
    tools = FakeToolClient()
    await memory.save_borrower(_verified_borrower())

    first = await handle_turn(
        _turn(f"call-{flow}", B_DUE, "request help"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert "detail" in first.reply_text.lower() or "request" in first.reply_text.lower()

    second = await handle_turn(
        _turn(f"call-{flow}", B_DUE, "50 percent settlement chahiye"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )

    assert prepare_action in second.actions_executed
    assert second.disposition == disposition
    assert second.transfer_to_human is True
    assert not reply_quotes_unauthorized_terms(second.reply_text)
    assert "authorized" in second.reply_text.lower() or "specialist" in second.reply_text.lower()

    borrower = await memory.load_borrower(B_DUE)
    assert any(note.get("type") == "negotiation_packet" for note in borrower.notes)


@pytest.mark.asyncio
async def test_conditional_pay_waiver_not_accepted_routed():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:conditional_pay]]"}])
    llm = ScriptedLLM(
        [
            [{"command": "start_flow", "flow": "conditional_pay"}],
            [
                {
                    "command": "set_slot",
                    "name": "negotiation_request",
                    "value": "pay karunga agar fee waive ho",
                }
            ],
        ]
    )
    tools = FakeToolClient()
    await memory.save_borrower(_verified_borrower())

    await handle_turn(
        _turn("call-cond", B_DUE, "conditional pay"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    response = await handle_turn(
        _turn("call-cond", B_DUE, "fee waive karo tabhi"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )

    assert "reject_conditional_waiver" in response.actions_executed
    assert response.disposition == "SETTLEMENT_REVIEW"
    assert response.transfer_to_human is True
    assert (
        "accept nahi" in response.reply_text.lower()
        or "condition" in response.reply_text.lower()
    )


@pytest.mark.asyncio
async def test_settlement_fishing_flagged_not_auto_rejected():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:settlement_review]]"}])
    llm = ScriptedLLM(
        [
            [{"command": "start_flow", "flow": "settlement_review"}],
            [
                {
                    "command": "set_slot",
                    "name": "negotiation_request",
                    "value": "one time settlement",
                }
            ],
        ]
    )
    tools = FakeToolClient()
    await memory.save_borrower(
        _verified_borrower(
            notes=[{"text": "one time settlement chahiye", "early": True}],
            payments=[{"date": "2026-05-01T10:00:00+00:00", "amount": 5000, "full": True}],
        )
    )

    await handle_turn(
        _turn("call-fish", B_DUE, "settlement"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    response = await handle_turn(
        _turn("call-fish", B_DUE, "discount chahiye"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )

    assert response.disposition == "SETTLEMENT_REVIEW"
    assert response.transfer_to_human is True
    borrower = await memory.load_borrower(B_DUE)
    packet = next(n for n in borrower.notes if n.get("type") == "negotiation_packet")
    assert packet.get("settlement_fishing_flagged") is True


@pytest.mark.asyncio
async def test_refusal_grievance_routes_dispute_seam():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:refusal_with_grievance]]"}])
    llm = ScriptedLLM([[{"command": "start_flow", "flow": "refusal_with_grievance"}]])
    tools = FakeToolClient()
    await memory.save_borrower(_verified_borrower())

    response = await handle_turn(
        _turn("call-griev", B_DUE, "galat charge hai isliye nahi dunga"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )

    assert "route_refusal_grievance" in response.actions_executed
    assert response.disposition == "REFUSAL_GRIEVANCE"
    state = await memory.load_state("call-griev")
    assert any(frame.flow == "dispute" for frame in state.flow_stack)
    assert state.flow_stack[-1].flow == "refusal_with_grievance" or any(
        f.flow == "dispute" and f.parked for f in state.flow_stack
    )
    borrower = await memory.load_borrower(B_DUE)
    assert any(note.get("type") == "refusal_grievance" for note in borrower.notes)


@pytest.mark.asyncio
async def test_genuine_hardship_restructure_not_strategic_default():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:restructure_review]]"}])
    llm = ScriptedLLM(
        [
            [{"command": "start_flow", "flow": "restructure_review"}],
            [{"command": "set_slot", "name": "negotiation_request", "value": "EMI kam karo"}],
        ]
    )
    tools = FakeToolClient()
    await memory.save_borrower(
        _verified_borrower(
            hardships=[{"type": "medical", "status": "corroborated", "onset": "2026-06-01"}],
        )
    )

    await handle_turn(
        _turn("call-hs-restruct", B_DUE, "restructure chahiye"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    response = await handle_turn(
        _turn("call-hs-restruct", B_DUE, "medical bills ki wajah se"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )

    state = await memory.load_state("call-hs-restruct")
    borrower = await memory.load_borrower(B_DUE)
    assert has_genuine_hardship_context(state, borrower)
    assert response.disposition == "RESTRUCTURE_REVIEW"
    assert response.disposition != "STRATEGIC_DEFAULT_WATCH"
    assert "prepare_restructure_review" in response.actions_executed
    assert response.transfer_to_human is True


def test_human_owned_negotiation_actions_recommend_only():
    state = new_conversation_state("c", "default", "b")
    state.slots["identity_ok"] = True
    state.slots["persona"] = {"ability": "low", "willingness": "low"}
    state = apply(state, [Command(command="start_flow", flow="settlement_review")])
    state = apply_decision_overlay(state, FLOWS)
    overlay = compute_overlay(state, FLOWS)
    assert "settlement_review" in overlay.human_recommendations
    assert overlay.selected_action not in HUMAN_OWNED_ACTIONS
