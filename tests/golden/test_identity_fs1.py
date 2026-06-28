"""FS-1 identity & entry flow tests."""

import re

import pytest

from app.clients.tools_sim import FakeToolClient
from app.engine.identity_gate import reply_discloses_debt
from app.engine.priority import reorder
from app.engine.retrieval import clear_retrieval_cache
from app.engine.tracker import apply, new_conversation_state
from app.engine.turn import handle_turn
from app.flows.loader import load_all_flows
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.schemas.command import Command
from app.schemas.state import BorrowerRecord
from tests.fixtures.test_borrowers import B_VERIFY_FAIL, B_VERIFY_OK
from tests.helpers.scripted_clients import ScriptedKB, ScriptedLLM

FLOWS = load_all_flows()

DEBT_PATTERN = re.compile(
    r"\b(5000|amount|due date|arrears|overdue|dpd|jama karna hoga)\b",
    re.IGNORECASE,
)


def _assert_no_debt_disclosure(text: str) -> None:
    assert not DEBT_PATTERN.search(text), f"Debt detail leaked in reply: {text!r}"


def _verify_borrower(borrower_id: str) -> BorrowerRecord:
    return BorrowerRecord(
        borrower_id=borrower_id,
        loan={"amount_due": 5000, "dpd": 30, "bucket": "0-30"},
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
async def test_verify_success_proceeds_collection_allowed():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:promise_to_pay]]"}])
    llm = ScriptedLLM(
        [
            [{"command": "set_slot", "name": "identity_response", "value": "4321"}],
            [
                {"command": "start_flow", "flow": "promise_to_pay"},
                {"command": "set_slot", "name": "ptp_date", "value": "2026-06-27"},
            ],
        ]
    )
    tools = FakeToolClient()
    tools.reset()
    await memory.save_borrower(_verify_borrower(B_VERIFY_OK))

    first = await handle_turn(
        _turn("call-verify-ok-1", B_VERIFY_OK, "mera last four 4321 hai"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert "verify_identity" in first.actions_executed
    assert "set_identity_ok" in first.actions_executed
    _assert_no_debt_disclosure(first.reply_text)

    state = await memory.load_state("call-verify-ok-1")
    assert state is not None
    assert state.slots.get("identity_ok") is True

    second = await handle_turn(
        _turn("call-verify-ok-1", B_VERIFY_OK, "kal payment kar dunga"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert "validate_ptp" in second.actions_executed
    assert second.reply_text
    assert "5000" in second.reply_text or "note" in second.reply_text.lower()


@pytest.mark.asyncio
async def test_verify_fail_once_retries_then_twice_closes_without_debt():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([])
    llm = ScriptedLLM(
        [
            [{"command": "set_slot", "name": "identity_response", "value": "0000"}],
            [{"command": "set_slot", "name": "identity_response", "value": "1111"}],
        ]
    )
    tools = FakeToolClient()
    tools.reset()
    await memory.save_borrower(_verify_borrower(B_VERIFY_FAIL))

    first = await handle_turn(
        _turn("call-verify-fail", B_VERIFY_FAIL, "galat digits 0000"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert "verify_identity" in first.actions_executed
    assert "incr_identity_attempts" in first.actions_executed
    assert "route_identity_failure" not in first.actions_executed
    assert "[COMPLIANCE-REVIEW]" in first.reply_text
    _assert_no_debt_disclosure(first.reply_text)

    second = await handle_turn(
        _turn("call-verify-fail", B_VERIFY_FAIL, "phir galat 1111"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert "route_identity_failure" in second.actions_executed
    assert second.transfer_to_human is True
    assert second.end_call is True
    _assert_no_debt_disclosure(second.reply_text)


@pytest.mark.asyncio
async def test_refuse_still_refuse_cannot_proceed_no_disclosure():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:identity_refused]]"}])
    llm = ScriptedLLM(
        [
            [{"command": "start_flow", "flow": "identity_refused"}],
            [{"command": "set_slot", "name": "identity_refusal_confirm", "value": "still_refuse"}],
        ]
    )
    tools = FakeToolClient()
    tools.reset()
    await memory.save_borrower(_verify_borrower(B_VERIFY_OK))

    first = await handle_turn(
        _turn("call-refuse-1", B_VERIFY_OK, "main verify nahi karunga"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert "[COMPLIANCE-REVIEW]" in first.reply_text
    _assert_no_debt_disclosure(first.reply_text)

    second = await handle_turn(
        _turn("call-refuse-1", B_VERIFY_OK, "nahi batana"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert "route_identity_failure" in second.actions_executed
    assert second.end_call is True
    _assert_no_debt_disclosure(second.reply_text)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flow_name", "transcript", "expected_reply_id"),
    [
        ("who_are_you", "aap kaun ho", "who_are_you_disclosure"),
        ("bot_disclosure", "kya ye bot hai", "bot_disclosure_message"),
        ("recording_disclosure", "call record ho rahi hai kya", "recording_disclosure_message"),
    ],
)
async def test_informational_disclosures_no_debt_detail(flow_name, transcript, expected_reply_id):
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": f"[[flow:{flow_name}]]"}])
    llm = ScriptedLLM([[{"command": "start_flow", "flow": flow_name}]])
    tools = FakeToolClient()
    tools.reset()
    await memory.save_borrower(_verify_borrower(B_VERIFY_OK))

    response = await handle_turn(
        _turn(f"call-{flow_name}", B_VERIFY_OK, transcript),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )

    assert "[COMPLIANCE-REVIEW]" in response.reply_text
    _assert_no_debt_disclosure(response.reply_text)
    assert FLOWS.responses[expected_reply_id][0].text.split("[COMPLIANCE-REVIEW]")[1].strip() in (
        response.reply_text.replace("[COMPLIANCE-REVIEW]", "").strip() or response.reply_text
    )


@pytest.mark.asyncio
async def test_collection_before_identity_ok_defers_no_debt():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:promise_to_pay]] kal"}])
    llm = ScriptedLLM([[{"command": "start_flow", "flow": "promise_to_pay"}]])
    tools = FakeToolClient()
    tools.reset()
    await memory.save_borrower(_verify_borrower(B_VERIFY_OK))

    response = await handle_turn(
        _turn("call-guard", B_VERIFY_OK, "kal payment kar dunga"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )

    state = await memory.load_state("call-guard")
    assert state is not None
    assert state.slots.get("identity_ok") is not True
    assert any(frame.flow == "identity_verification" for frame in state.flow_stack)
    _assert_no_debt_disclosure(response.reply_text)
    assert "pehchaan verify" in response.reply_text.lower() or (
        "[COMPLIANCE-REVIEW]" in response.reply_text
    )


def test_identity_beats_dispute_on_priority_ladder():
    state = new_conversation_state("c-id", "default", "b")
    state = apply(
        state,
        [
            Command(command="start_flow", flow="dispute"),
            Command(command="start_flow", flow="identity_verification"),
        ],
    )
    reorder(state, FLOWS)

    assert state.flow_stack[-1].flow == "identity_verification"
    assert state.flow_stack[-1].parked is False
    assert state.flow_stack[0].flow == "dispute"
    assert state.flow_stack[0].parked is True


@pytest.mark.asyncio
async def test_vulnerability_preempts_identity_no_debt():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([])
    llm = ScriptedLLM([[]])
    tools = FakeToolClient()
    tools.reset()
    await memory.save_borrower(_verify_borrower(B_VERIFY_OK))

    response = await handle_turn(
        _turn("call-vul-id", B_VERIFY_OK, "Main suicide soch raha hoon"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )

    assert response.transfer_to_human is True
    _assert_no_debt_disclosure(response.reply_text)
    assert llm.call_count == 0


@pytest.mark.asyncio
async def test_opt_out_preempts_identity_via_gate():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([])
    llm = ScriptedLLM([[]])
    tools = FakeToolClient()
    tools.reset()
    borrower = _verify_borrower(B_VERIFY_OK)
    borrower.compliance_flags = {"opt_out": True}
    await memory.save_borrower(borrower)

    response = await handle_turn(
        _turn("call-opt-id", B_VERIFY_OK, "hello"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )

    assert response.reply_text == ""
    _assert_no_debt_disclosure(response.reply_text)


def test_gate_blocks_pre_verification_debt_disclosure():
    from app.config import tenant_config
    from app.engine.gate import gate

    state = new_conversation_state("c-gate", "default", "b")
    state.slots["amount_due"] = 5000
    result = gate(
        "Please pay 5000 by due date",
        state,
        tenant_config("default"),
    )
    assert result.verdict == "block"
    assert result.reason == "pre_verification_debt_disclosure"
    assert not reply_discloses_debt(result.text, state)
