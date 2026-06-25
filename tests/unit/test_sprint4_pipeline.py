import json
from unittest.mock import AsyncMock

import pytest

from app.engine.pipeline import transcript_to_commands
from app.engine.retrieval import clear_retrieval_cache
from app.engine.tracker import new_conversation_state
from app.schemas.command import Command

PROMISE_FLOW = {
    "name": "promise_to_pay",
    "description": "Borrower agrees to pay on a future date (kal, parso, next week).",
    "score": 0.92,
}
DISPUTE_FLOW = {
    "name": "dispute",
    "description": "Borrower disputes the loan, amount, or prior payments.",
    "score": 0.85,
}


def _state(today: str = "2026-06-25"):
    state = new_conversation_state("c", "default", "b")
    state.slots["call_date"] = today
    return state


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_retrieval_cache()
    yield
    clear_retrieval_cache()


@pytest.mark.asyncio
async def test_pipeline_kb_to_commands():
    mock_kb = AsyncMock()
    mock_kb.retrieve.return_value = [
        {
            "doc_id": "1",
            "score": 0.9,
            "text": "[[flow:promise_to_pay]] kal payment",
        }
    ]
    mock_llm = AsyncMock()
    mock_llm.complete.return_value = json.dumps(
        [
            {"command": "start_flow", "flow": "promise_to_pay"},
            {"command": "set_slot", "name": "ptp_date", "value": "2026-06-26"},
        ]
    )
    state = _state()
    commands = await transcript_to_commands(
        "kal paisa de dunga",
        state,
        "default",
        kb_client=mock_kb,
        llm_client=mock_llm,
    )
    names = {(cmd.command, cmd.flow, cmd.name) for cmd in commands}
    assert ("start_flow", "promise_to_pay", None) in names
    assert ("set_slot", None, "ptp_date") in names
    mock_kb.retrieve.assert_awaited_once()


@pytest.mark.asyncio
async def test_pipeline_multi_signal_candidates():
    mock_kb = AsyncMock()
    mock_kb.retrieve.return_value = [
        {
            "doc_id": "1",
            "score": 0.9,
            "text": "[[flow:promise_to_pay]] kal payment",
        },
        {
            "doc_id": "2",
            "score": 0.85,
            "text": "[[flow:dispute]] wrong amount",
        },
    ]
    mock_llm = AsyncMock()
    mock_llm.complete.return_value = json.dumps(
        [
            {"command": "start_flow", "flow": "dispute"},
            {"command": "start_flow", "flow": "promise_to_pay"},
            {"command": "set_slot", "name": "ptp_date", "value": "2026-06-26"},
        ]
    )
    state = _state()
    commands = await transcript_to_commands(
        "galat amount hai kal de dunga",
        state,
        "default",
        kb_client=mock_kb,
        llm_client=mock_llm,
    )
    flows = {cmd.flow for cmd in commands if cmd.command == "start_flow"}
    assert flows == {"dispute", "promise_to_pay"}


@pytest.mark.asyncio
async def test_pipeline_kb_fail_soft_yields_clarify():
    mock_kb = AsyncMock()
    mock_kb.retrieve.return_value = []
    mock_llm = AsyncMock()
    mock_llm.complete.return_value = json.dumps([{"command": "totally_invalid"}])
    state = _state()
    commands = await transcript_to_commands(
        "hello",
        state,
        "default",
        kb_client=mock_kb,
        llm_client=mock_llm,
    )
    assert commands == [Command(command="clarify")]
