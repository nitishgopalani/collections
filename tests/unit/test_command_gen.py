import json
from datetime import date
from unittest.mock import AsyncMock

import pytest

from app.engine.command_gen import (
    build_system_prompt,
    build_user_prompt,
    generate,
    parse_and_validate_commands,
    resolve_today,
    slots_for_llm_prompt,
)
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
VULNERABILITY_FLOW = {
    "name": "vulnerability",
    "description": "Borrower signals hardship or crisis.",
    "score": 0.7,
}
PAY_NOW_FLOW = {
    "name": "pay_now",
    "description": "Borrower agrees to pay immediately or today.",
    "score": 0.6,
}


def _state(today: str = "2026-06-25"):
    state = new_conversation_state("c", "default", "b")
    state.slots["call_date"] = today
    state.slots["amount_due"] = 5000
    return state


def test_system_prompt_has_no_policy_rules():
    prompt = build_system_prompt("2026-06-25").lower()
    forbidden = [
        "compliance gate",
        "rbi",
        "attempt cap",
        "call-window",
        "threaten",
        "opt-out",
        "harassment",
    ]
    for phrase in forbidden:
        assert phrase not in prompt


def test_parse_rejects_malformed_and_unknown_commands():
    junk = json.dumps(
        [
            {"command": "start_flow", "flow": "promise_to_pay", "extra": "drop"},
            {"command": "illegal_action"},
            {"command": "set_slot", "name": "unknown_slot", "value": 1},
        ]
    )
    result = parse_and_validate_commands(
        junk,
        candidate_flows=[PROMISE_FLOW],
    )
    assert result == [Command(command="start_flow", flow="promise_to_pay")]


def test_parse_empty_returns_clarify():
    assert parse_and_validate_commands("[]") == [Command(command="clarify")]
    assert parse_and_validate_commands("not json") == [Command(command="clarify")]


@pytest.mark.asyncio
async def test_generate_gibberish_returns_clarify():
    mock_llm = AsyncMock()
    mock_llm.complete.return_value = json.dumps([{"command": "totally_invalid"}])
    state = _state()
    flows = [PROMISE_FLOW, DISPUTE_FLOW]
    result = await generate("asdf qwerty zzz", state, flows, llm=mock_llm)
    assert result == [Command(command="clarify")]


@pytest.mark.asyncio
async def test_generate_rejects_hallucinated_junk_from_llm():
    mock_llm = AsyncMock()
    mock_llm.complete.return_value = json.dumps(
        [
            {"command": "start_flow", "flow": "promise_to_pay"},
            {"command": "send_reply", "text": "Hello sir"},
            {"command": "set_slot", "name": "ptp_date", "value": "2026-06-27"},
        ]
    )
    state = _state()
    result = await generate("kal payment kar dunga", state, [PROMISE_FLOW], llm=mock_llm)
    commands = {(cmd.command, cmd.flow, cmd.name) for cmd in result}
    assert ("start_flow", "promise_to_pay", None) in commands
    assert ("set_slot", None, "ptp_date") in commands
    assert all(cmd.command != "send_reply" for cmd in result)


@pytest.mark.asyncio
async def test_generate_date_resolution_to_iso():
    mock_llm = AsyncMock()
    mock_llm.complete.return_value = json.dumps(
        [
            {"command": "start_flow", "flow": "promise_to_pay"},
            {"command": "set_slot", "name": "ptp_date", "value": "2026-06-27"},
        ]
    )
    state = _state(today="2026-06-25")
    await generate("kal paisa de dunga", state, [PROMISE_FLOW], llm=mock_llm)
    user_payload = json.loads(mock_llm.complete.call_args.args[1])
    assert user_payload["slots"]["call_date"] == "2026-06-25"
    system = mock_llm.complete.call_args.args[0]
    assert "2026-06-25" in system


@pytest.mark.asyncio
async def test_generate_multi_signal_mock():
    mock_llm = AsyncMock()
    mock_llm.complete.return_value = json.dumps(
        [
            {"command": "start_flow", "flow": "dispute"},
            {"command": "start_flow", "flow": "promise_to_pay"},
            {"command": "set_slot", "name": "dispute_reason", "value": "already paid"},
        ]
    )
    state = _state()
    flows = [DISPUTE_FLOW, PROMISE_FLOW]
    result = await generate(
        "maine pay kar diya par parso dekhunga",
        state,
        flows,
        llm=mock_llm,
    )
    flow_starts = [cmd.flow for cmd in result if cmd.command == "start_flow"]
    assert "dispute" in flow_starts
    assert "promise_to_pay" in flow_starts


def test_resolve_today_from_state_slot():
    state = _state("2026-01-15")
    assert resolve_today(state) == "2026-01-15"


def test_resolve_today_defaults_to_calendar_today():
    state = new_conversation_state("c", "t", "b")
    assert resolve_today(state) == date.today().isoformat()


def test_slots_for_llm_prompt_hides_read_only_context():
    state = _state()
    state.slots["borrower_name"] = "Rajesh"
    state.slots["amount_due"] = 350
    state.slots["identity_ok"] = True
    visible = slots_for_llm_prompt(state.slots)
    assert "borrower_name" not in visible
    assert "amount_due" not in visible
    assert "identity_ok" not in visible
    assert visible["call_date"] == "2026-06-25"


def test_build_user_prompt_includes_identity_response_hint():
    state = _state()
    from app.schemas.state import Frame

    state.flow_stack = [Frame(flow="identity_verification", step_index=0)]
    payload = json.loads(build_user_prompt("mera last four 4321", [PROMISE_FLOW], state))
    hints = payload["active_flow_slot_hints"]
    assert hints
    assert hints[0]["slot"] == "identity_response"
    assert "borrower_name" not in payload["slots"]
