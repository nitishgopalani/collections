import pytest

from app.clients.tools_sim import FakeToolClient
from app.engine.actions import make_action_runner
from app.engine.executor import run
from app.engine.tracker import apply
from app.schemas.command import Command
from tests.fixtures.test_borrowers import B_DUE, B_PAID
from tests.golden.test_executor_helpers import (
    FLOWS,
    _base_state,
    _stack_dispute_over_promise,
)


@pytest.fixture
def sim_tools():
    client = FakeToolClient()
    client.reset()
    return client


@pytest.fixture
def action_runner(sim_tools):
    return make_action_runner(sim_tools)


def test_ptp_within_policy_confirm_and_schedule(action_runner):
    state = _base_state("promise_to_pay")
    state = apply(state, [Command(command="set_slot", name="ptp_date", value="2026-06-27")])
    result = run(state, FLOWS, action_runner)

    assert result.question_slot is None
    assert result.reply_id == "confirm_ptp"
    assert "validate_ptp" in result.actions_called
    assert "schedule_followup" in result.actions_called
    assert result.state.slots["followup_scheduled"] is True
    assert result.state.slots["ptp_allowed"] is True


def test_ptp_too_far_counter(action_runner):
    state = _base_state("promise_to_pay")
    state = apply(state, [Command(command="set_slot", name="ptp_date", value="2026-08-15")])
    result = run(state, FLOWS, action_runner)

    assert result.reply_id == "ask_earlier_date"
    assert "schedule_followup" not in result.actions_called
    assert result.state.slots["ptp_allowed"] is False


def test_collect_pauses_then_resumes_next_turn(action_runner):
    state = _base_state("promise_to_pay")
    paused = run(state, FLOWS, action_runner)
    assert paused.question_slot == "ptp_date"
    assert paused.reply_id is None

    resumed_state = apply(
        paused.state,
        [Command(command="set_slot", name="ptp_date", value="2026-06-28")],
    )
    resumed = run(resumed_state, FLOWS, action_runner)
    assert resumed.question_slot is None
    assert resumed.reply_id == "confirm_ptp"


def test_dispute_pause_and_acknowledge_branch(action_runner):
    state = _base_state("dispute", borrower_id=B_DUE)
    paused = run(state, FLOWS, action_runner)
    assert paused.question_slot == "dispute_type"

    with_type = apply(
        paused.state,
        [Command(command="set_slot", name="dispute_type", value="prior_payment")],
    )
    paused_reason = run(with_type, FLOWS, action_runner)
    assert paused_reason.question_slot == "dispute_reason"

    resumed_state = apply(
        paused_reason.state,
        [Command(command="set_slot", name="dispute_reason", value="already paid")],
    )
    result = run(resumed_state, FLOWS, action_runner)
    assert result.reply_id == "dispute_ack"
    assert result.state.slots["dispute_logged"] is True
    assert "verify_payment" in result.actions_called
    assert result.state.slots["compliance_flags"].get("dispute_hold") is True


def test_dispute_handoff_branch_without_reason(action_runner):
    state = _base_state("dispute", borrower_id=B_DUE)
    state = apply(
        state,
        [
            Command(command="set_slot", name="dispute_type", value="prior_payment"),
            Command(command="set_slot", name="dispute_reason", value=""),
        ],
    )
    result = run(state, FLOWS, action_runner)
    assert result.reply_id == "dispute_handoff"
    assert result.state.slots["dispute_logged"] is False


def test_vulnerability_hard_stop_transfer(action_runner):
    state = _base_state("vulnerability")
    result = run(state, FLOWS, action_runner)
    assert result.reply_id == "vulnerability_care"
    assert result.transfer_to_human is True
    assert result.state.slots["vulnerable_routed"] is True


def test_executor_determinism(action_runner):
    state = _base_state("promise_to_pay")
    state = apply(state, [Command(command="set_slot", name="ptp_date", value="2026-06-27")])

    first = run(state.model_copy(deep=True), FLOWS, action_runner)
    second = run(state.model_copy(deep=True), FLOWS, action_runner)

    assert first.reply_id == second.reply_id
    assert first.actions_called == second.actions_called
    assert first.question_slot == second.question_slot


def test_dispute_paid_borrower_payment_found_handoff(action_runner):
    state = _base_state("dispute", borrower_id=B_PAID)
    state = apply(
        state,
        [
            Command(command="set_slot", name="dispute_type", value="prior_payment"),
            Command(command="set_slot", name="dispute_reason", value="already paid"),
        ],
    )
    result = run(state, FLOWS, action_runner)

    assert result.reply_id == "payment_already_received"
    assert result.state.slots["payment_found"] is True
    assert result.transfer_to_human is True
    assert result.state.slots["dispute_dropped"] is True


def test_dispute_due_borrower_resumes_parked_promise(action_runner):
    state = _stack_dispute_over_promise(B_DUE)
    state = apply(
        state,
        [
            Command(command="set_slot", name="dispute_type", value="prior_payment"),
            Command(command="set_slot", name="dispute_reason", value="wrong amount"),
        ],
    )
    result = run(state, FLOWS, action_runner)

    assert result.state.slots["payment_found"] is False
    assert result.state.slots.get("dispute_dropped") is True
    assert len(result.state.flow_stack) == 1
    assert result.state.flow_stack[0].flow == "promise_to_pay"
    assert result.state.flow_stack[0].parked is False
    assert result.reply_id is None
    assert result.question_slot == "ptp_date"
