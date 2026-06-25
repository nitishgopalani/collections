"""FS-2 payment breadth flow tests."""

import re

import pytest

from app.clients.tools_sim import FakeToolClient
from app.engine.actions import ActionRegistry, make_action_runner
from app.engine.executor import run
from app.engine.priority import reorder
from app.engine.retrieval import clear_retrieval_cache
from app.engine.tracker import apply, new_conversation_state
from app.engine.turn import handle_turn
from app.flows.loader import load_all_flows
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.schemas.command import Command
from app.schemas.state import BorrowerRecord
from tests.fixtures.test_borrowers import B_DUE, B_PROCESSING, B_VERIFY_OK
from tests.helpers.scripted_clients import ScriptedKB, ScriptedLLM

FLOWS = load_all_flows()

DEBT_PATTERN = re.compile(
    r"\b(5000|4200|amount due|due date|arrears|principal|interest)\b",
    re.IGNORECASE,
)


def _assert_no_debt_disclosure(text: str) -> None:
    assert not DEBT_PATTERN.search(text), f"Debt detail leaked in reply: {text!r}"


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
async def test_partial_capture_balance_ptp_scheduled():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:partial_payment]]"}])
    llm = ScriptedLLM(
        [
            [{"command": "start_flow", "flow": "partial_payment"}],
            [{"command": "set_slot", "name": "partial_amount", "value": 2000}],
            [{"command": "set_slot", "name": "ptp_date", "value": "2026-06-28"}],
        ]
    )
    tools = FakeToolClient()
    tools.reset()
    await memory.save_borrower(_verified_borrower())

    first = await handle_turn(
        _turn("call-partial-1", B_DUE, "2000 abhi de sakta hoon"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert "partial" in first.reply_text.lower() or "[COMPLIANCE-REVIEW]" in first.reply_text

    second = await handle_turn(
        _turn("call-partial-1", B_DUE, "2000"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert "validate_partial" in second.actions_executed
    assert "create_payment_link" in second.actions_executed
    assert "push_ptp_for_balance" in second.actions_executed
    assert "log_disposition" in second.actions_executed
    assert second.disposition == "PARTIAL_CAPTURED"

    state = await memory.load_state("call-partial-1")
    assert state is not None
    assert any(frame.flow == "promise_to_pay" for frame in state.flow_stack)
    assert state.slots.get("balance_remaining") == 3000

    third = await handle_turn(
        _turn("call-partial-1", B_DUE, "28 June baaki de dunga"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert "schedule_followup" in third.actions_executed


@pytest.mark.asyncio
async def test_partial_amount_over_due_rejected():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:partial_payment]]"}])
    llm = ScriptedLLM(
        [
            [{"command": "start_flow", "flow": "partial_payment"}],
            [{"command": "set_slot", "name": "partial_amount", "value": 8000}],
        ]
    )
    tools = FakeToolClient()
    tools.reset()
    await memory.save_borrower(_verified_borrower())

    await handle_turn(
        _turn("call-partial-reject", B_DUE, "partial payment"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    response = await handle_turn(
        _turn("call-partial-reject", B_DUE, "8000 de dunga"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )

    assert "validate_partial" in response.actions_executed
    assert "create_payment_link" not in response.actions_executed
    assert (
        "valid nahi" in response.reply_text.lower()
        or "[COMPLIANCE-REVIEW]" in response.reply_text
    )


@pytest.mark.asyncio
async def test_partial_payment_link_idempotent():
    tools = FakeToolClient()
    tools.reset()
    registry = ActionRegistry(tools)
    state = new_conversation_state("c-partial-idem", "default", B_DUE)
    state.slots["amount_due"] = 5000
    state.slots["partial_amount"] = 2000
    state.slots["identity_ok"] = True
    state = await registry.run_async("validate_partial", state)

    first = await registry.run_async("create_payment_link", state)
    second = await registry.run_async("create_payment_link", first)

    assert first.slots["payment_link"] == second.slots["payment_link"]
    assert tools.write_effect_count("create_payment_link") == 1


@pytest.mark.asyncio
async def test_already_initiated_found_no_reask_utr_captured():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:already_initiated]]"}])
    llm = ScriptedLLM(
        [
            [
                {"command": "start_flow", "flow": "already_initiated"},
                {"command": "set_slot", "name": "utr_reference", "value": "UTR123456"},
            ],
        ]
    )
    tools = FakeToolClient()
    tools.reset()
    await memory.save_borrower(
        BorrowerRecord(
            borrower_id=B_PROCESSING,
            loan={"amount_due": 5000, "dpd": 10, "bucket": "0-30"},
            identity={"identity_ok": True},
        )
    )

    response = await handle_turn(
        _turn("call-initiated", B_PROCESSING, "payment ho chuka hai processing mein hai"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )

    assert "verify_payment" in response.actions_executed
    assert "create_payment_link" not in response.actions_executed
    assert response.disposition == "PAYMENT_CONFIRMED"
    assert (
        "processing" in response.reply_text.lower()
        or "[COMPLIANCE-REVIEW]" in response.reply_text
    )

    state = await memory.load_state("call-initiated")
    assert state is not None
    assert state.slots.get("utr_captured") is True


@pytest.mark.asyncio
async def test_already_initiated_not_found_routes_dispute():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:already_initiated]]"}])
    llm = ScriptedLLM([[{"command": "start_flow", "flow": "already_initiated"}]])
    tools = FakeToolClient()
    tools.reset()
    await memory.save_borrower(_verified_borrower())

    response = await handle_turn(
        _turn("call-init-notfound", B_DUE, "maine kal payment kar diya hai"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )

    assert "verify_payment" in response.actions_executed
    assert "route_to_dispute" in response.actions_executed
    assert "create_payment_link" not in response.actions_executed

    state = await memory.load_state("call-init-notfound")
    assert state is not None
    assert any(frame.flow == "dispute" for frame in state.flow_stack)
    assert state.slots.get("routed_from_already_initiated") is True


@pytest.mark.asyncio
async def test_dues_breakup_post_verification():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:dues_breakup]]"}])
    llm = ScriptedLLM([[{"command": "start_flow", "flow": "dues_breakup"}]])
    tools = FakeToolClient()
    tools.reset()
    await memory.save_borrower(_verified_borrower())

    response = await handle_turn(
        _turn("call-breakup", B_DUE, "breakup kya hai amount ka"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )

    assert "lookup_dues_breakup" in response.actions_executed
    assert "principal" in response.reply_text.lower()
    assert "[COMPLIANCE-REVIEW]" in response.reply_text


@pytest.mark.asyncio
async def test_dues_breakup_blocked_pre_verification():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:dues_breakup]]"}])
    llm = ScriptedLLM([[{"command": "start_flow", "flow": "dues_breakup"}]])
    tools = FakeToolClient()
    tools.reset()
    await memory.save_borrower(
        BorrowerRecord(
            borrower_id=B_VERIFY_OK,
            loan={"amount_due": 5000, "dpd": 30, "bucket": "0-30"},
        )
    )

    response = await handle_turn(
        _turn("call-breakup-blocked", B_VERIFY_OK, "amount breakup batao"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )

    _assert_no_debt_disclosure(response.reply_text)
    assert "4200" not in response.reply_text


@pytest.mark.asyncio
async def test_alt_channel_link_for_rail_idempotent():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:alt_channel]]"}])
    llm = ScriptedLLM(
        [
            [{"command": "start_flow", "flow": "alt_channel"}],
            [{"command": "set_slot", "name": "payment_rail", "value": "upi"}],
        ]
    )
    tools = FakeToolClient()
    tools.reset()
    await memory.save_borrower(_verified_borrower())

    await handle_turn(
        _turn("call-alt", B_DUE, "UPI link bhejo"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    response = await handle_turn(
        _turn("call-alt", B_DUE, "upi"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )

    assert "create_payment_link" in response.actions_executed
    assert "upi" in response.reply_text.lower()
    assert tools.write_effect_count("create_payment_link") == 1

    state = await memory.load_state("call-alt")
    assert state is not None
    assert state.slots.get("payment_link_rail") == "upi"


@pytest.mark.asyncio
async def test_alt_channel_idempotent_same_turn_rerun():
    tools = FakeToolClient()
    tools.reset()
    registry = ActionRegistry(tools)
    state = new_conversation_state("c-alt-idem", "default", B_DUE)
    state.slots["amount_due"] = 5000
    state.slots["payment_rail"] = "app"
    state.slots["identity_ok"] = True

    first = await registry.run_async("create_payment_link", state)
    second = await registry.run_async("create_payment_link", first)

    assert first.slots["payment_link"] == second.slots["payment_link"]
    assert tools.write_effect_count("create_payment_link") == 1


def test_partial_plus_dispute_dispute_wins():
    state = new_conversation_state("c-multi", "default", "b")
    state = apply(
        state,
        [
            Command(command="start_flow", flow="partial_payment"),
            Command(command="start_flow", flow="dispute"),
        ],
    )
    reorder(state, FLOWS)

    assert state.flow_stack[-1].flow == "dispute"
    assert state.flow_stack[-1].parked is False
    assert state.flow_stack[0].flow == "partial_payment"
    assert state.flow_stack[0].parked is True


def test_partial_payment_executor_end_to_end():
    runner = make_action_runner(FakeToolClient())
    tools = FakeToolClient()
    tools.reset()
    runner = make_action_runner(tools)
    state = new_conversation_state("c-partial-ex", "default", B_DUE)
    state.slots["amount_due"] = 5000
    state.slots["identity_ok"] = True
    state = apply(
        state,
        [
            Command(command="start_flow", flow="partial_payment"),
            Command(command="set_slot", name="partial_amount", value=2000),
        ],
    )
    result = run(state, FLOWS, runner)

    assert "validate_partial" in result.actions_called
    assert "create_payment_link" in result.actions_called
    assert "push_ptp_for_balance" in result.actions_called
    assert result.state.slots.get("disposition") == "PARTIAL_CAPTURED"
    assert any(frame.flow == "promise_to_pay" for frame in result.state.flow_stack)
