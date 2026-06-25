"""End-to-end handle_turn integration tests (Sprint 7)."""

import pytest

from app.clients.tools_sim import FakeToolClient
from app.engine.retrieval import clear_retrieval_cache
from app.engine.turn import handle_turn
from app.memory.audit import query_turn_audits_by_borrower
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.schemas.state import BorrowerRecord
from tests.fixtures.test_borrowers import B_DUE
from tests.helpers.scripted_clients import ScriptedKB, ScriptedLLM

PROMISE_KB = [
    {
        "doc_id": "1",
        "score": 0.9,
        "text": "[[flow:promise_to_pay]] kal payment",
    }
]

DISPUTE_KB = [
    {
        "doc_id": "2",
        "score": 0.85,
        "text": "[[flow:dispute]] wrong amount",
    }
]

MULTI_KB = PROMISE_KB + DISPUTE_KB


def _turn_request(
    call_id: str,
    borrower_id: str,
    transcript: str,
    *,
    tenant_id: str = "default",
) -> TurnRequest:
    return TurnRequest(
        call_id=call_id,
        tenant_id=tenant_id,
        borrower_id=borrower_id,
        transcript=transcript,
        turn_meta={"call_date": "2026-06-25"},
    )


def _borrower_due(borrower_id: str = B_DUE) -> BorrowerRecord:
    return BorrowerRecord(
        borrower_id=borrower_id,
        loan={"amount_due": 5000, "dpd": 45, "bucket": "30-60"},
    )


@pytest.fixture(autouse=True)
def _clear_retrieval_cache():
    clear_retrieval_cache()
    yield
    clear_retrieval_cache()


@pytest.mark.asyncio
async def test_handle_turn_ptp_confirm_e2e():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB(PROMISE_KB)
    llm = ScriptedLLM(
        [
            [
                {"command": "start_flow", "flow": "promise_to_pay"},
                {"command": "set_slot", "name": "ptp_date", "value": "2026-06-27"},
            ]
        ]
    )
    tools = FakeToolClient()
    tools.reset()

    request = _turn_request("call-ptp-e2e", B_DUE, "kal paisa de dunga")
    await memory.save_borrower(_borrower_due())
    request.turn_meta["call_date"] = "2026-06-25"

    response = await handle_turn(
        request,
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )

    assert llm.call_count == 1
    assert "validate_ptp" in response.actions_executed
    assert "schedule_followup" in response.actions_executed
    assert response.reply_text
    assert "2026-06-27" in response.reply_text or "note" in response.reply_text.lower()
    assert response.state_version == 1

    audits = await query_turn_audits_by_borrower(memory, B_DUE)
    assert len(audits) == 1
    chain = audits[0]
    assert chain.candidate_flows[0]["name"] == "promise_to_pay"
    assert chain.llm_calls == 1
    assert chain.gate_verdict in {"allow", "modify"}


@pytest.mark.asyncio
async def test_handle_turn_collect_then_resume():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB(PROMISE_KB)
    llm = ScriptedLLM(
        [
            [{"command": "start_flow", "flow": "promise_to_pay"}],
            [{"command": "set_slot", "name": "ptp_date", "value": "2026-06-28"}],
        ]
    )
    tools = FakeToolClient()
    tools.reset()
    await memory.save_borrower(_borrower_due())

    first = await handle_turn(
        _turn_request("call-collect-1", B_DUE, "kal de dunga"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert "kab payment" in first.reply_text.lower() or "date" in first.reply_text.lower()

    second = await handle_turn(
        _turn_request("call-collect-1", B_DUE, "28 June kar dunga"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert second.state_version == 2
    assert llm.call_count == 2
    assert "schedule_followup" in second.actions_executed


@pytest.mark.asyncio
async def test_handle_turn_retrieval_cache_skips_duplicate_kb():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB(PROMISE_KB)
    llm = ScriptedLLM(
        [
            [{"command": "clarify"}],
            [{"command": "clarify"}],
        ]
    )
    tools = FakeToolClient()
    await memory.save_borrower(_borrower_due())

    transcript = "same transcript for cache"
    for call_id in ("cache-a", "cache-b"):
        await handle_turn(
            _turn_request(call_id, B_DUE, transcript),
            memory=memory,
            kb=kb,
            llm=llm,
            tools=tools,
        )

    assert kb.retrieve_calls == 1


@pytest.mark.asyncio
async def test_handle_turn_out_of_window_silent():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB(PROMISE_KB)
    llm = ScriptedLLM([[{"command": "start_flow", "flow": "promise_to_pay"}]])
    tools = FakeToolClient()
    await memory.save_borrower(_borrower_due())

    from unittest.mock import patch

    with patch("app.engine.gate.within_call_window", return_value=False):
        response = await handle_turn(
            _turn_request("call-late", B_DUE, "kal de dunga"),
            memory=memory,
            kb=kb,
            llm=llm,
            tools=tools,
        )

    assert response.reply_text == ""
