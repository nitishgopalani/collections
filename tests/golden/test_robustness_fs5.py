"""FS-5 robustness flows + consolidated QA tests."""

import re

import pytest

from app.clients.tools_sim import FakeToolClient
from app.engine.actions import make_async_action_runner
from app.engine.executor import run_async as run_executor_async
from app.engine.retrieval import clear_retrieval_cache
from app.engine.tracker import apply, new_conversation_state
from app.engine.turn import handle_turn
from app.flows.loader import load_all_flows
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.schemas.command import Command
from app.schemas.state import BorrowerRecord
from tests.fixtures.test_borrowers import B_DUE, B_VERIFY_OK
from tests.helpers.scripted_clients import ScriptedKB, ScriptedLLM

FLOWS = load_all_flows()

DEBT_PATTERN = re.compile(
    r"\b(5000|amount|due date|arrears|overdue|dpd|principal|interest|charges)\b",
    re.IGNORECASE,
)


def _assert_no_debt_disclosure(text: str) -> None:
    assert not DEBT_PATTERN.search(text), f"Debt detail leaked: {text!r}"


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
async def test_unrecognized_utterance_clarifies_and_conversation_continues():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([])
    llm = ScriptedLLM([[], [{"command": "start_flow", "flow": "balance_inquiry"}]])
    tools = FakeToolClient()
    tools.reset()
    call_id = "call-clarify-fallback"
    await memory.save_borrower(_verified_borrower())

    first = await handle_turn(
        _turn(call_id, B_DUE, "blorp florp zzz"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert first.reply_text
    assert first.end_call is False

    second = await handle_turn(
        _turn(call_id, B_DUE, "balance batao"),
        memory=memory,
        kb=ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:balance_inquiry]]"}]),
        llm=ScriptedLLM([[{"command": "start_flow", "flow": "balance_inquiry"}]]),
        tools=tools,
    )
    assert "balance" in second.reply_text.lower() or "hazaar" in second.reply_text.lower()


@pytest.mark.asyncio
async def test_human_handoff_reachable_via_command():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([])
    llm = ScriptedLLM([[{"command": "human_handoff"}]])
    tools = FakeToolClient()
    await memory.save_borrower(_verified_borrower())

    response = await handle_turn(
        _turn("call-handoff-cmd", B_DUE, "mujhe insaan se baat karni hai"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert response.transfer_to_human is True
    assert response.reply_text


@pytest.mark.asyncio
async def test_human_handoff_flow_from_any_state():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:human_handoff_request]]"}])
    llm = ScriptedLLM(
        [
            [{"command": "start_flow", "flow": "hardship"}],
            [{"command": "start_flow", "flow": "human_handoff_request"}],
        ]
    )
    tools = FakeToolClient()
    call_id = "call-handoff-flow"
    await memory.save_borrower(_verified_borrower())

    await handle_turn(
        _turn(call_id, B_DUE, "medical kharcha bahut hai"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    response = await handle_turn(
        _turn(call_id, B_DUE, "agent se baat karo"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert response.transfer_to_human is True
    state = await memory.load_state(call_id)
    assert state.slots.get("disposition") == "HUMAN_HANDOFF"


@pytest.mark.asyncio
async def test_repeat_request_re_utters_last_collect_prompt():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB(
        [
            {"doc_id": "1", "score": 0.9, "text": "[[flow:promise_to_pay]]"},
            {"doc_id": "2", "score": 0.88, "text": "[[flow:repeat_request]] repeat"},
        ]
    )
    llm = ScriptedLLM(
        [
            [{"command": "start_flow", "flow": "promise_to_pay"}],
            [{"command": "start_flow", "flow": "repeat_request"}],
        ]
    )
    tools = FakeToolClient()
    call_id = "call-repeat"
    await memory.save_borrower(_verified_borrower())

    first = await handle_turn(
        _turn(call_id, B_DUE, "payment karunga"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert "payment" in first.reply_text.lower() or "date" in first.reply_text.lower()

    second = await handle_turn(
        _turn(call_id, B_DUE, "dobara bolo"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert second.reply_text
    assert "payment" in second.reply_text.lower() or "date" in second.reply_text.lower()


@pytest.mark.asyncio
async def test_repeat_critical_slot_confirms_after_second_repeat():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB(
        [
            {"doc_id": "1", "score": 0.9, "text": "[[flow:promise_to_pay]]"},
            {"doc_id": "2", "score": 0.88, "text": "[[flow:repeat_request]]"},
        ]
    )
    llm = ScriptedLLM(
        [
            [{"command": "start_flow", "flow": "promise_to_pay"}],
            [{"command": "start_flow", "flow": "repeat_request"}],
            [{"command": "start_flow", "flow": "repeat_request"}],
        ]
    )
    tools = FakeToolClient()
    call_id = "call-repeat-critical"
    await memory.save_borrower(_verified_borrower())

    await handle_turn(
        _turn(call_id, B_DUE, "payment karunga"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    await handle_turn(
        _turn(call_id, B_DUE, "dobara bolo"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    response = await handle_turn(
        _turn(call_id, B_DUE, "phir se nahi samjha"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert "confirm" in response.reply_text.lower() or "sahi" in response.reply_text.lower()


@pytest.mark.asyncio
async def test_out_of_scope_honest_limit():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:out_of_scope]]"}])
    llm = ScriptedLLM([[{"command": "start_flow", "flow": "out_of_scope"}]])
    tools = FakeToolClient()
    await memory.save_borrower(_verified_borrower())

    response = await handle_turn(
        _turn("call-oos", B_DUE, "stock market mein invest kaise karun"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert "madad nahi" in response.reply_text.lower() or "account" in response.reply_text.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "flow,expected_fragment",
    [
        ("balance_inquiry", "balance"),
        ("due_date_inquiry", "due date"),
        ("loan_terms_inquiry", "months"),
    ],
)
async def test_informational_lookups_post_identity(flow, expected_fragment):
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": f"[[flow:{flow}]]"}])
    llm = ScriptedLLM([[{"command": "start_flow", "flow": flow}]])
    tools = FakeToolClient()
    tools.reset()
    await memory.save_borrower(_verified_borrower(B_VERIFY_OK))

    response = await handle_turn(
        _turn(f"call-{flow}", B_VERIFY_OK, "account info"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert expected_fragment in response.reply_text.lower()


@pytest.mark.asyncio
async def test_balance_lookup_blocked_pre_identity():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:balance_inquiry]]"}])
    llm = ScriptedLLM([[{"command": "start_flow", "flow": "balance_inquiry"}]])
    tools = FakeToolClient()
    await memory.save_borrower(
        BorrowerRecord(borrower_id=B_VERIFY_OK, loan={"amount_due": 5000, "dpd": 30})
    )

    response = await handle_turn(
        _turn("call-bal-pre-id", B_VERIFY_OK, "kitna due hai"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    _assert_no_debt_disclosure(response.reply_text)


@pytest.mark.asyncio
async def test_cross_flow_opt_out_during_hardship():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB(
        [
            {"doc_id": "1", "score": 0.9, "text": "[[flow:hardship]]"},
            {"doc_id": "2", "score": 0.95, "text": "[[flow:opt_out]] stop"},
        ]
    )
    llm = ScriptedLLM(
        [
            [
                {"command": "start_flow", "flow": "hardship"},
                {"command": "start_flow", "flow": "opt_out"},
            ]
        ]
    )
    tools = FakeToolClient()
    call_id = "call-opt-hardship"
    await memory.save_borrower(_verified_borrower())

    response = await handle_turn(
        _turn(call_id, B_DUE, "naukri gayi stop calling"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    state = await memory.load_state(call_id)
    assert state.slots["compliance_flags"]["opt_out"] is True
    assert response.reply_text


@pytest.mark.asyncio
async def test_cross_flow_dispute_during_partial_payment():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([])
    llm = ScriptedLLM(
        [
            [
                {"command": "start_flow", "flow": "partial_payment"},
                {"command": "start_flow", "flow": "dispute"},
                {"command": "set_slot", "name": "partial_amount", "value": "1000"},
            ]
        ]
    )
    tools = FakeToolClient()
    call_id = "call-dispute-partial"
    await memory.save_borrower(_verified_borrower())

    response = await handle_turn(
        _turn(call_id, B_DUE, "1000 partial but galat amount"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    state = await memory.load_state(call_id)
    assert any(frame.flow == "dispute" for frame in state.flow_stack)
    assert response.reply_text


@pytest.mark.asyncio
async def test_cross_flow_distress_during_identity_no_debt():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([])
    llm = ScriptedLLM([[]])
    tools = FakeToolClient()
    await memory.save_borrower(
        BorrowerRecord(borrower_id=B_VERIFY_OK, loan={"amount_due": 5000, "dpd": 30})
    )

    response = await handle_turn(
        _turn("call-distress-id", B_VERIFY_OK, "Main suicide soch raha hoon"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert response.transfer_to_human is True
    _assert_no_debt_disclosure(response.reply_text)


@pytest.mark.asyncio
async def test_cross_flow_third_party_during_collection():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([])
    llm = ScriptedLLM(
        [
            [
                {"command": "start_flow", "flow": "promise_to_pay"},
                {"command": "start_flow", "flow": "third_party"},
            ]
        ]
    )
    tools = FakeToolClient()
    call_id = "call-tp-ptp"
    await memory.save_borrower(_verified_borrower())

    response = await handle_turn(
        _turn(call_id, B_DUE, "kal dunga main unki biwi hoon"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    _assert_no_debt_disclosure(response.reply_text)
    state = await memory.load_state(call_id)
    assert state.slots.get("third_party_active") is True
    assert any(frame.flow == "third_party" and not frame.parked for frame in state.flow_stack)


@pytest.mark.compliance
def test_adversarial_gate_still_blocks_threats_with_robustness_flows():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.config import tenant_config
    from app.engine.gate import gate

    state = new_conversation_state("c", "default", B_DUE)
    state.slots["identity_ok"] = True
    state = apply(state, [Command(command="start_flow", flow="balance_inquiry")])
    cfg = tenant_config("default")
    now = datetime(2026, 6, 25, 10, 0, tzinfo=ZoneInfo(cfg.call_window_timezone))
    result = gate("Police aa jayegi agar EMI nahi doge", state, cfg, now=now)
    assert result.verdict in {"block", "modify"}
    assert "police" not in result.text.lower()


@pytest.mark.asyncio
async def test_repeat_flow_single_turn_no_executor_loop():
    state = new_conversation_state("c", "default", B_DUE)
    state.slots["identity_ok"] = True
    state.slots["last_reply_id"] = "ask_ptp_date"
    state = apply(state, [Command(command="start_flow", flow="repeat_request")])
    runner = make_async_action_runner(FakeToolClient())
    result = await run_executor_async(state, FLOWS, runner)
    assert "prepare_repeat_prompt" in result.actions_called
    assert "set_repeat_reply_from_last" in result.actions_called
    assert len(result.actions_called) <= 4


def test_robustness_flows_loaded():
    for name in (
        "repeat_request",
        "out_of_scope",
        "human_handoff_request",
        "balance_inquiry",
        "due_date_inquiry",
        "loan_terms_inquiry",
    ):
        assert name in FLOWS.flows
