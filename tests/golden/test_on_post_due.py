"""On-Due and Post-Due SOT scripts: scenario dispatch, push ladders, commit, transfer."""

from datetime import date, timedelta

import pytest

from app.clients.tools_sim import FakeToolClient
from app.engine.actions import make_async_action_runner
from app.engine.executor import run_async as run_executor_async
from app.engine.tracker import apply, new_conversation_state
from app.memory.test_borrower import hardcoded_test_borrower
from app.flows.loader import load_all_flows
from app.schemas.command import Command
from app.schemas.state import Frame

FLOWS = load_all_flows()


def _state(**slots):
    state = new_conversation_state("call-1", "salary_on_time", "borrower-1")
    state.slots.update(slots)
    return state


def _runner():
    return make_async_action_runner(FakeToolClient())


async def _run(state):
    return await run_executor_async(state, FLOWS, _runner())


# ---------------------------------------------------------------------------
# Scenario selection (select_sot_scenario) + test borrower wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_select_scenario_future_is_pre():
    runner = _runner()
    today = date.today()
    state = _state(
        call_date=today.isoformat(),
        due_date=(today + timedelta(days=5)).isoformat(),
    )
    state = await runner("select_sot_scenario", state)
    assert state.slots["sot_scenario"] == "pre"


@pytest.mark.asyncio
async def test_select_scenario_today_is_on_due():
    runner = _runner()
    today = date.today()
    state = _state(call_date=today.isoformat(), due_date=today.isoformat())
    state = await runner("select_sot_scenario", state)
    assert state.slots["sot_scenario"] == "on_due"


@pytest.mark.asyncio
async def test_select_scenario_past_is_post_due():
    runner = _runner()
    today = date.today()
    state = _state(
        call_date=today.isoformat(),
        due_date=(today - timedelta(days=4)).isoformat(),
    )
    state = await runner("select_sot_scenario", state)
    assert state.slots["sot_scenario"] == "post_due"


def test_test_borrower_due_date_follows_scenario():
    today = date.today()
    assert hardcoded_test_borrower(scenario="pre").loan["due_date"] > today.isoformat()
    assert hardcoded_test_borrower(scenario="on_due").loan["due_date"] == today.isoformat()
    post = hardcoded_test_borrower(scenario="post_due").loan
    assert post["due_date"] < today.isoformat()
    # On/Post-due carry no live discount so the shared confirm lines read correctly.
    assert post["offer_amount"] == post["repay_amount"]
    assert post["discount_amount"] == 0


# ---------------------------------------------------------------------------
# On-Due
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_due_willing_goes_to_commit_today():
    state = _state(sot_scenario="on_due")
    state = apply(state, [Command(command="start_flow", flow="sotod_offer")])
    await _run(state)  # utters sotod_offer, pauses at payment-intent
    state = apply(
        state, [Command(command="set_slot", name="sot_payment_intent", value="willing")]
    )
    await _run(state)  # chains to sot_commit, pauses asking the time
    state = apply(
        state, [Command(command="set_slot", name="sot_customer_time", value="shaam 5 baje")]
    )
    result = await _run(state)
    assert result.reply_id == "sot_confirm_today"


@pytest.mark.asyncio
async def test_on_due_push_ladder_then_commit():
    state = _state(sot_scenario="on_due")
    state = apply(state, [Command(command="start_flow", flow="sotod_offer")])
    await _run(state)
    # Refuse the offer -> enter the 3-push ladder.
    state = apply(
        state, [Command(command="set_slot", name="sot_payment_intent", value="refused")]
    )
    await _run(state)  # ask_reason collect
    state = apply(
        state, [Command(command="set_slot", name="sot_payment_problem", value="paisa nahi hai")]
    )
    r1 = await _run(state)
    assert r1.reply_id == "sotod_push1"
    state = apply(
        state, [Command(command="set_slot", name="sot_payment_intent_2", value="refused")]
    )
    r2 = await _run(state)
    assert r2.reply_id == "sotod_push2"
    state = apply(
        state, [Command(command="set_slot", name="sot_payment_intent_3", value="refused")]
    )
    r3 = await _run(state)
    assert r3.reply_id == "sotod_push3"
    # Agree on the last push -> commit today, ask time.
    state = apply(
        state, [Command(command="set_slot", name="sot_payment_intent_4", value="willing")]
    )
    await _run(state)
    state = apply(
        state, [Command(command="set_slot", name="sot_customer_time", value="dopahar")]
    )
    result = await _run(state)
    assert result.reply_id == "sot_confirm_today"


@pytest.mark.asyncio
async def test_on_due_already_paid_closes():
    state = _state(sot_scenario="on_due")
    state = apply(state, [Command(command="start_flow", flow="sotod_offer")])
    await _run(state)
    state = apply(
        state,
        [Command(command="set_slot", name="sot_payment_intent", value="already_paid")],
    )
    result = await _run(state)
    assert result.reply_id == "sot_already_paid"
    assert result.end_call is True


@pytest.mark.asyncio
async def test_on_due_after_due_commitment_warns_then_transfers():
    # Scenario-aware after-due warning + transfer on continued refusal.
    state = _state(sot_scenario="on_due", sot_commit_timing="after_due")
    state = apply(state, [Command(command="start_flow", flow="sot_commit")])
    r = await _run(state)
    assert r.reply_id == "sotod_afterdue_warning"
    state = apply(
        state, [Command(command="set_slot", name="sot_afterdue_decision", value="later")]
    )
    result = await _run(state)
    assert result.transfer_to_human is True


# ---------------------------------------------------------------------------
# Post-Due
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_due_four_push_ladder_then_commit():
    state = _state(sot_scenario="post_due")
    state = apply(state, [Command(command="start_flow", flow="sotpd_offer")])
    await _run(state)
    state = apply(
        state, [Command(command="set_slot", name="sot_payment_intent", value="refused")]
    )
    await _run(state)  # ask_reason
    state = apply(
        state, [Command(command="set_slot", name="sot_payment_problem", value="salary nahi aayi")]
    )
    assert (await _run(state)).reply_id == "sotpd_push1"
    state = apply(
        state, [Command(command="set_slot", name="sot_payment_intent_2", value="refused")]
    )
    assert (await _run(state)).reply_id == "sotpd_push2"
    state = apply(
        state, [Command(command="set_slot", name="sot_payment_intent_3", value="refused")]
    )
    assert (await _run(state)).reply_id == "sotpd_push3"
    state = apply(
        state, [Command(command="set_slot", name="sot_payment_intent_4", value="refused")]
    )
    assert (await _run(state)).reply_id == "sotpd_push4"
    # Agree on the 4th push -> commit today.
    state = apply(
        state, [Command(command="set_slot", name="sot_payment_intent_5", value="willing")]
    )
    await _run(state)
    state = apply(
        state, [Command(command="set_slot", name="sot_customer_time", value="raat tak")]
    )
    assert (await _run(state)).reply_id == "sot_confirm_today"


@pytest.mark.asyncio
async def test_post_due_after_due_uses_post_due_warning():
    state = _state(sot_scenario="post_due", sot_commit_timing="after_due")
    state = apply(state, [Command(command="start_flow", flow="sot_commit")])
    r = await _run(state)
    assert r.reply_id == "sotpd_afterdue_warning"


@pytest.mark.asyncio
async def test_pre_closure_after_due_still_uses_pre_warning():
    # Regression: default scenario keeps the original pre-closure warning.
    state = _state(sot_commit_timing="after_due")
    state = apply(state, [Command(command="start_flow", flow="sot_commit")])
    r = await _run(state)
    assert r.reply_id == "sot_afterdue_warning"


@pytest.mark.asyncio
async def test_opener_dispatches_by_scenario_post_due():
    # Confirm identity on a post-due borrower -> opener routes into sotpd_offer.
    today = date.today()
    state = _state(
        call_date=today.isoformat(),
        due_date=(today - timedelta(days=4)).isoformat(),
    )
    state.flow_stack = [Frame(flow="sot_opener", step_index=0)]
    state = apply(
        state,
        [Command(command="set_slot", name="sot_identity_response", value="confirmed")],
    )
    result = await _run(state)
    # After identity, opener selects scenario and chains into the post-due offer.
    assert result.state.slots.get("sot_scenario") == "post_due"
    assert result.reply_id == "sotpd_offer"
