"""FS-8 dispute breadth tests."""

from datetime import UTC, datetime

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import tenant_config
from app.engine.gate import gate
from app.engine.hardship import reply_has_pressure_language
from app.engine.priority import reorder
from app.engine.retrieval import clear_retrieval_cache
from app.engine.tracker import apply, new_conversation_state
from app.engine.turn import handle_turn
from app.flows.loader import load_all_flows
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.schemas.command import Command
from app.schemas.state import BorrowerRecord
from tests.fixtures.test_borrowers import (
    B_CLOSED,
    B_DUE,
    B_NACH_BORROWER,
    B_NACH_LENDER,
    B_OVERDUE,
)
from tests.helpers.scripted_clients import ScriptedKB, ScriptedLLM

FLOWS = load_all_flows()
_REF = datetime(2026, 6, 25, 10, 0, tzinfo=UTC)


def _verified_borrower(borrower_id: str = B_DUE, **extra) -> BorrowerRecord:
    base = {
        "borrower_id": borrower_id,
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


async def _run_dispute_branch(
    memory,
    *,
    call_id: str,
    borrower_id: str,
    dispute_type: str,
    claim: str | None = None,
    reason: str | None = None,
):
    llm_batches = [[{"command": "start_flow", "flow": "dispute"}]]
    llm_batches.append([{"command": "set_slot", "name": "dispute_type", "value": dispute_type}])
    if claim is not None:
        llm_batches.append([{"command": "set_slot", "name": "dispute_claim", "value": claim}])
    if reason is not None:
        llm_batches.append([{"command": "set_slot", "name": "dispute_reason", "value": reason}])

    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": f"[[flow:dispute]] {dispute_type}"}])
    llm = ScriptedLLM(llm_batches)
    tools = FakeToolClient()
    tools.reset()

    response = None
    for index in range(len(llm_batches)):
        response = await handle_turn(
            _turn(call_id, borrower_id, f"dispute step {index}"),
            memory=memory,
            kb=kb,
            llm=llm,
            tools=tools,
        )
    return response


@pytest.mark.asyncio
async def test_amount_dispute_verify_before_act_borrower_wrong():
    memory = InMemoryMemoryStore()
    await memory.save_borrower(_verified_borrower())
    response = await _run_dispute_branch(
        memory,
        call_id="call-amt-wrong",
        borrower_id=B_DUE,
        dispute_type="amount",
        claim="2000 hi due hona chahiye",
    )
    assert "verify_amount_dispute" in response.actions_executed
    assert response.disposition == "DISPUTE_AMOUNT"
    assert not reply_has_pressure_language(response.reply_text)
    assert (
        "accusation" not in response.reply_text.lower()
        or "[COMPLIANCE-REVIEW]" in response.reply_text
    )


@pytest.mark.asyncio
async def test_amount_dispute_charges_borrower_right_routed():
    memory = InMemoryMemoryStore()
    await memory.save_borrower(_verified_borrower())
    response = await _run_dispute_branch(
        memory,
        call_id="call-amt-right",
        borrower_id=B_DUE,
        dispute_type="amount",
        claim="charges galat hain",
    )
    assert "verify_amount_dispute" in response.actions_executed
    assert response.disposition == "DISPUTE_AMOUNT"
    assert response.transfer_to_human is True
    assert not reply_has_pressure_language(response.reply_text)


@pytest.mark.asyncio
async def test_loan_closed_borrower_right_warm_close():
    memory = InMemoryMemoryStore()
    await memory.save_borrower(
        _verified_borrower(borrower_id=B_CLOSED, loan={"amount_due": 0, "dpd": 0})
    )
    response = await _run_dispute_branch(
        memory,
        call_id="call-closed-yes",
        borrower_id=B_CLOSED,
        dispute_type="loan_closed",
    )
    assert "verify_loan_status" in response.actions_executed
    assert response.disposition == "DISPUTE_CLOSED"
    assert not reply_has_pressure_language(response.reply_text)


@pytest.mark.asyncio
async def test_loan_closed_borrower_wrong_neutral_route():
    memory = InMemoryMemoryStore()
    await memory.save_borrower(_verified_borrower())
    response = await _run_dispute_branch(
        memory,
        call_id="call-closed-no",
        borrower_id=B_DUE,
        dispute_type="loan_closed",
    )
    assert "verify_loan_status" in response.actions_executed
    assert response.disposition == "DISPUTE_CLOSED"
    assert response.transfer_to_human is True


@pytest.mark.asyncio
async def test_not_due_borrower_right_apology():
    memory = InMemoryMemoryStore()
    await memory.save_borrower(_verified_borrower())
    response = await _run_dispute_branch(
        memory,
        call_id="call-notdue-yes",
        borrower_id=B_DUE,
        dispute_type="not_due_yet",
    )
    assert "verify_not_due_yet" in response.actions_executed
    assert response.disposition == "DISPUTE_NOT_DUE"
    assert not reply_has_pressure_language(response.reply_text)


@pytest.mark.asyncio
async def test_not_due_borrower_wrong_factual_clarify():
    memory = InMemoryMemoryStore()
    await memory.save_borrower(_verified_borrower(borrower_id=B_OVERDUE))
    response = await _run_dispute_branch(
        memory,
        call_id="call-notdue-no",
        borrower_id=B_OVERDUE,
        dispute_type="not_due_yet",
    )
    assert "verify_not_due_yet" in response.actions_executed
    assert response.disposition == "DISPUTE_NOT_DUE"
    assert not reply_has_pressure_language(response.reply_text)


@pytest.mark.asyncio
async def test_nach_lender_fault_operational_route():
    memory = InMemoryMemoryStore()
    await memory.save_borrower(_verified_borrower(borrower_id=B_NACH_LENDER))
    response = await _run_dispute_branch(
        memory,
        call_id="call-nach-l",
        borrower_id=B_NACH_LENDER,
        dispute_type="nach",
    )
    assert "verify_nach_debit" in response.actions_executed
    assert response.disposition == "DISPUTE_NACH"
    assert response.transfer_to_human is True
    assert (
        "galti nahi" in response.reply_text.lower() or "[COMPLIANCE-REVIEW]" in response.reply_text
    )


@pytest.mark.asyncio
async def test_nach_borrower_side_explain_retry():
    memory = InMemoryMemoryStore()
    await memory.save_borrower(_verified_borrower(borrower_id=B_NACH_BORROWER))
    response = await _run_dispute_branch(
        memory,
        call_id="call-nach-b",
        borrower_id=B_NACH_BORROWER,
        dispute_type="nach",
    )
    assert "verify_nach_debit" in response.actions_executed
    assert response.disposition == "DISPUTE_NACH"
    assert not reply_has_pressure_language(response.reply_text)


@pytest.mark.asyncio
async def test_double_charge_human_route_no_auto_resolution():
    memory = InMemoryMemoryStore()
    await memory.save_borrower(_verified_borrower())
    response = await _run_dispute_branch(
        memory,
        call_id="call-double",
        borrower_id=B_DUE,
        dispute_type="double_charge",
        claim="do baar charge hua",
    )
    assert "prepare_double_charge_review" in response.actions_executed
    assert response.disposition == "DOUBLE_CHARGE_REVIEW"
    assert response.transfer_to_human is True
    assert "create_payment_link" not in response.actions_executed


@pytest.mark.asyncio
async def test_dispute_hold_suppresses_dunning_subsequent_turn():
    memory = InMemoryMemoryStore()
    await memory.save_borrower(_verified_borrower())
    await _run_dispute_branch(
        memory,
        call_id="call-hold",
        borrower_id=B_DUE,
        dispute_type="not_due_yet",
    )
    state = await memory.load_state("call-hold")
    assert state.slots["compliance_flags"].get("dispute_hold") is True
    cfg = tenant_config("default")
    result = gate("Please EMI jama karna hoga abhi", state, cfg, now=_REF)
    assert result.verdict == "block"
    assert result.reason in {"dispute_hold_no_pressure", "dunning_suppressed"}


@pytest.mark.asyncio
async def test_already_initiated_not_found_routes_prior_payment_dispute():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:already_initiated]]"}])
    llm = ScriptedLLM(
        [
            [{"command": "start_flow", "flow": "already_initiated"}],
            [{"command": "set_slot", "name": "dispute_reason", "value": "paid yesterday"}],
        ]
    )
    tools = FakeToolClient()
    tools.reset()
    await memory.save_borrower(_verified_borrower())

    await handle_turn(
        _turn("call-init-disp", B_DUE, "payment ho chuka hai"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    state = await memory.load_state("call-init-disp")
    assert state.slots.get("dispute_type") == "prior_payment"
    assert state.slots.get("routed_from_already_initiated") is True
    assert any(frame.flow == "dispute" for frame in state.flow_stack)

    response = await handle_turn(
        _turn("call-init-disp", B_DUE, "paid yesterday proof"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert "verify_payment" in response.actions_executed


def test_dispute_beats_ptp_on_priority_ladder():
    state = new_conversation_state("c", "default", "b")
    state = apply(
        state,
        [
            Command(command="start_flow", flow="promise_to_pay"),
            Command(command="start_flow", flow="dispute"),
        ],
    )
    reorder(state, FLOWS)
    assert state.flow_stack[-1].flow == "dispute"
    assert state.flow_stack[0].flow == "promise_to_pay"
    assert state.flow_stack[0].parked is True


def test_opt_out_preempts_dispute_on_priority_ladder():
    state = new_conversation_state("c", "default", "b")
    state = apply(
        state,
        [
            Command(command="start_flow", flow="dispute"),
            Command(command="start_flow", flow="opt_out"),
        ],
    )
    reorder(state, FLOWS)
    assert state.flow_stack[-1].flow == "opt_out"


def test_vulnerable_preempts_dispute_on_priority_ladder():
    state = new_conversation_state("c", "default", "b")
    state = apply(
        state,
        [
            Command(command="start_flow", flow="dispute"),
            Command(command="start_flow", flow="vulnerability"),
        ],
    )
    reorder(state, FLOWS)
    assert state.flow_stack[-1].flow == "vulnerability"
