"""P4 — attempt-indexed response variants + escalate_to."""

from app.clients.tools_sim import FakeToolClient
from app.engine.actions import make_action_runner
from app.engine.executor import run
from app.engine.nlg import REPLY_COUNTS_KEY, clear_reply_counts, render_resolved
from app.engine.robustness import record_outbound_context
from app.schemas.flow import Flow, FlowSet, FlowStep, ResponseTemplate
from app.schemas.state import ConversationState, Frame


def _flows_with_escalate() -> FlowSet:
    return FlowSet(
        flows={
            "obj_demo": Flow(
                description="two-attempt objection with escalate",
                priority="dispute",
                steps=[
                    FlowStep(
                        id="say",
                        utter="obj_demo_line",
                        next="end",
                        escalate_to="do_hangup",
                    ),
                    FlowStep(id="do_hangup", action="hangup_call", next="end"),
                ],
            )
        },
        responses={
            "obj_demo_line": [
                ResponseTemplate(text="first try", language="en", attempt=1),
                ResponseTemplate(text="second try", language="en", attempt=2),
            ]
        },
    )


def _flows_hold_only() -> FlowSet:
    return FlowSet(
        flows={
            "obj_hold": Flow(
                description="two-attempt objection without escalate_to",
                priority="dispute",
                steps=[FlowStep(utter="obj_hold_line", next="end")],
            )
        },
        responses={
            "obj_hold_line": [
                ResponseTemplate(text="hold-1", language="en", attempt=1),
                ResponseTemplate(text="hold-2", language="en", attempt=2),
            ]
        },
    )


def test_two_attempt_group_plays_first_then_second_then_escalates():
    flows = _flows_with_escalate()
    runner = make_action_runner(FakeToolClient())
    state = ConversationState(
        call_id="p4-esc",
        tenant_id="test",
        borrower_id="b1",
        slots={},
        flow_stack=[Frame(flow="obj_demo")],
    )

    # Play 1 → attempt 1
    r1 = run(state, flows, action_runner=runner)
    assert r1.reply_id == "obj_demo_line"
    text1 = render_resolved("obj_demo_line", r1.state, flows, locale="en-IN").text
    assert text1 == "first try"
    state = record_outbound_context(
        r1.state, reply_id="obj_demo_line", question_slot=None, draft=text1
    )
    assert state.slots[REPLY_COUNTS_KEY]["obj_demo_line"] == 1

    # Play 2 → attempt 2
    state.flow_stack = [Frame(flow="obj_demo")]
    r2 = run(state, flows, action_runner=runner)
    assert r2.reply_id == "obj_demo_line"
    text2 = render_resolved("obj_demo_line", r2.state, flows, locale="en-IN").text
    assert text2 == "second try"
    state = record_outbound_context(
        r2.state, reply_id="obj_demo_line", question_slot=None, draft=text2
    )
    assert state.slots[REPLY_COUNTS_KEY]["obj_demo_line"] == 2

    # Play 3 → escalate_to hangup (highest already played)
    state.flow_stack = [Frame(flow="obj_demo")]
    r3 = run(state, flows, action_runner=runner)
    assert r3.reply_id != "obj_demo_line"
    assert r3.end_call is True
    assert r3.state.slots.get("sot_call_closed") is True
    assert REPLY_COUNTS_KEY not in r3.state.slots


def test_two_attempt_group_holds_at_second_without_escalate_to():
    flows = _flows_hold_only()
    runner = make_action_runner(FakeToolClient())
    state = ConversationState(
        call_id="p4-hold",
        tenant_id="test",
        borrower_id="b1",
        slots={},
        flow_stack=[Frame(flow="obj_hold")],
    )
    for expected in ("hold-1", "hold-2", "hold-2"):
        result = run(state, flows, action_runner=runner)
        assert result.reply_id == "obj_hold_line"
        text = render_resolved("obj_hold_line", result.state, flows, locale="en-IN").text
        assert text == expected
        state = record_outbound_context(
            result.state, reply_id="obj_hold_line", question_slot=None, draft=text
        )
        state.flow_stack = [Frame(flow="obj_hold")]


def test_reply_counters_cleared_on_sot_call_closed():
    slots = {REPLY_COUNTS_KEY: {"obj_demo_line": 2}}
    clear_reply_counts(slots)
    assert REPLY_COUNTS_KEY not in slots

    runner = make_action_runner(FakeToolClient())
    state = ConversationState(
        call_id="p4-clear",
        tenant_id="test",
        borrower_id="b1",
        slots={REPLY_COUNTS_KEY: {"obj_demo_line": 2}},
        flow_stack=[],
    )
    out = runner("hangup_call", state)
    assert out.slots.get("sot_call_closed") is True
    assert REPLY_COUNTS_KEY not in out.slots
