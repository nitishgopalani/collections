"""FS-4 compliance & handoff flow tests."""

import re

import pytest

from app.clients.tools_sim import FakeToolClient
from app.engine.actions import make_action_runner
from app.engine.compliance_handoff import reply_discloses_debt_or_arrears
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
from tests.fixtures.test_borrowers import B_DUE, B_VERIFY_OK
from tests.helpers.scripted_clients import ScriptedKB, ScriptedLLM

FLOWS = load_all_flows()

DEBT_PATTERN = re.compile(
    r"\b(5000|amount|due date|arrears|overdue|dpd|loan|emi|jama karna|outstanding)\b",
    re.IGNORECASE,
)

ACCUSATION_PATTERN = re.compile(
    r"\b(fraud|scam|fake|jhoot|jhooth|accuse|criminal)\b",
    re.IGNORECASE,
)


def _assert_no_debt_disclosure(text: str) -> None:
    assert not DEBT_PATTERN.search(text), f"Debt detail leaked: {text!r}"


def _assert_no_accusation(text: str) -> None:
    assert not ACCUSATION_PATTERN.search(text), f"Accusatory language: {text!r}"


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
async def test_opt_out_sets_flag_and_confirms_then_gate_silent():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:opt_out]] stop calling"}])
    llm = ScriptedLLM([[{"command": "start_flow", "flow": "opt_out"}]])
    tools = FakeToolClient()
    tools.reset()
    call_id = "call-opt-out-fs4"
    await memory.save_borrower(_verified_borrower(B_VERIFY_OK))

    first = await handle_turn(
        _turn(call_id, B_VERIFY_OK, "stop calling me"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert "[COMPLIANCE-REVIEW]" in first.reply_text
    assert "contact nahi" in first.reply_text.lower()

    state = await memory.load_state(call_id)
    assert state.slots["compliance_flags"]["opt_out"] is True
    assert state.slots.get("disposition") == "OPT_OUT"

    borrower = await memory.load_borrower(B_VERIFY_OK)
    assert borrower.compliance_flags.get("opt_out") is True

    llm2 = ScriptedLLM([[]])
    second = await handle_turn(
        _turn(call_id, B_VERIFY_OK, "hello again"),
        memory=memory,
        kb=kb,
        llm=llm2,
        tools=tools,
    )
    assert second.reply_text == ""


@pytest.mark.parametrize(
    "contact_type,response,expect_handoff",
    [
        ("wrong_number", "galat number hai", False),
        ("family_member", "nahi wrong number", False),
        ("someone_else", "someone else", False),
        ("minor", None, True),
    ],
)
@pytest.mark.asyncio
async def test_third_party_never_discloses_debt(contact_type, response, expect_handoff):
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:third_party]]"}])
    turn_one = [
        {"command": "start_flow", "flow": "third_party"},
        {"command": "set_slot", "name": "third_party_contact_type", "value": contact_type},
    ]
    llm_batches: list[list[dict]] = [turn_one]
    if response is not None:
        llm_batches.append(
            [{"command": "set_slot", "name": "third_party_borrower_check", "value": response}]
        )
    llm = ScriptedLLM(llm_batches)
    tools = FakeToolClient()
    tools.reset()
    call_id = f"call-tp-{contact_type}"
    await memory.save_borrower(_verified_borrower(B_VERIFY_OK))

    first = await handle_turn(
        _turn(call_id, B_VERIFY_OK, "hello"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    _assert_no_debt_disclosure(first.reply_text)

    if response is not None:
        second = await handle_turn(
            _turn(call_id, B_VERIFY_OK, response),
            memory=memory,
            kb=kb,
            llm=llm,
            tools=tools,
        )
        _assert_no_debt_disclosure(second.reply_text)
        response_turn = second
    else:
        response_turn = first

    if expect_handoff:
        assert response_turn.transfer_to_human is True
    elif response is not None:
        state = await memory.load_state(call_id)
        assert state.slots.get("confirmed_not_borrower") is True
        assert state.slots["compliance_flags"].get("number_suppressed") is True


def test_third_party_gate_blocks_debt_even_when_identity_ok():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.config import tenant_config

    state = new_conversation_state("c", "default", "b")
    state.slots["identity_ok"] = True
    state.slots["third_party_active"] = True
    state.slots["amount_due"] = 5000

    cfg = tenant_config("default")
    now = datetime(2026, 6, 25, 10, 0, tzinfo=ZoneInfo(cfg.call_window_timezone))
    result = gate("Aapka loan overdue hai 5000", state, cfg, now=now)
    assert result.verdict == "block"
    assert "third_party" in result.reason or "pre_verification" in result.reason


@pytest.mark.asyncio
async def test_fraud_halt_no_accusation_handoff():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:fraud_claim]]"}])
    llm = ScriptedLLM([[{"command": "start_flow", "flow": "fraud_claim"}]])
    tools = FakeToolClient()
    await memory.save_borrower(_verified_borrower())

    response = await handle_turn(
        _turn("call-fraud", B_DUE, "maine ye loan liya hi nahi"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    _assert_no_accusation(response.reply_text)
    assert response.transfer_to_human is True
    state = await memory.load_state("call-fraud")
    assert state.slots["compliance_flags"].get("fraud_investigation") is True
    assert state.slots.get("disposition") == "FRAUD_CLAIM"


@pytest.mark.asyncio
async def test_lawyer_legal_handoff():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:lawyer_rep]]"}])
    llm = ScriptedLLM([[{"command": "start_flow", "flow": "lawyer_rep"}]])
    tools = FakeToolClient()
    await memory.save_borrower(_verified_borrower())

    response = await handle_turn(
        _turn("call-lawyer", B_DUE, "main unka lawyer bol raha hoon"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert response.transfer_to_human is True
    state = await memory.load_state("call-lawyer")
    assert state.slots["compliance_flags"].get("legal_handoff") is True


@pytest.mark.asyncio
async def test_deceased_care_first_suppresses_dunning():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:deceased_borrower]]"}])
    llm = ScriptedLLM([[{"command": "start_flow", "flow": "deceased_borrower"}]])
    tools = FakeToolClient()
    await memory.save_borrower(_verified_borrower())

    response = await handle_turn(
        _turn("call-deceased", B_DUE, "woh mar chuke hain"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert response.transfer_to_human is True
    _assert_no_debt_disclosure(response.reply_text)
    state = await memory.load_state("call-deceased")
    assert state.slots.get("dunning_suppressed") is True
    assert state.slots.get("deceased_reported") is True


@pytest.mark.asyncio
async def test_harassment_logged_escalated_no_pressure():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:harassment_complaint]]"}])
    llm = ScriptedLLM([[{"command": "start_flow", "flow": "harassment_complaint"}]])
    tools = FakeToolClient()
    await memory.save_borrower(_verified_borrower())

    response = await handle_turn(
        _turn("call-harass", B_DUE, "yeh harassment hai"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert response.transfer_to_human is True
    assert not reply_has_pressure_language(response.reply_text)
    state = await memory.load_state("call-harass")
    assert state.slots.get("harassment_complaint_logged") is True
    borrower = await memory.load_borrower(B_DUE)
    assert any(n.get("type") == "harassment_complaint" for n in borrower.notes)


def test_opt_out_preempts_hardship_on_priority_ladder():
    state = new_conversation_state("c-id", "default", "b")
    state = apply(
        state,
        [
            Command(command="start_flow", flow="hardship"),
            Command(command="start_flow", flow="opt_out"),
        ],
    )
    reorder(state, FLOWS)

    assert state.flow_stack[-1].flow == "opt_out"
    assert state.flow_stack[-1].parked is False
    assert state.flow_stack[0].flow == "hardship"
    assert state.flow_stack[0].parked is True


def test_apply_opt_out_local_action():
    runner = make_action_runner(FakeToolClient())
    state = new_conversation_state("c", "default", "b")
    updated = runner("apply_opt_out", state)
    assert updated.slots["compliance_flags"]["opt_out"] is True
    assert updated.slots["disposition"] == "OPT_OUT"
    assert updated.slots.get("opt_out_ack_this_turn") is True


def test_reply_discloses_debt_third_party_context():
    state = new_conversation_state("c", "default", "b")
    state.slots["identity_ok"] = True
    state.slots["confirmed_not_borrower"] = True
    assert reply_discloses_debt_or_arrears("Aapke arrears clear karein", state)
