from app.engine.priority import reorder
from app.engine.tracker import apply, new_conversation_state
from app.flows.loader import load_all_flows
from app.schemas.command import Command

FLOWS = load_all_flows()


def test_dispute_then_promise_reorder_dispute_active():
    state = new_conversation_state("c-pri", "default", "b")
    state = apply(
        state,
        [
            Command(command="start_flow", flow="dispute"),
            Command(command="start_flow", flow="promise_to_pay"),
        ],
    )
    reorder(state, FLOWS)

    assert len(state.flow_stack) == 2
    assert state.flow_stack[-1].flow == "dispute"
    assert state.flow_stack[-1].parked is False
    assert state.flow_stack[0].flow == "promise_to_pay"
    assert state.flow_stack[0].parked is True


def test_promise_then_dispute_same_ordering():
    state = new_conversation_state("c-pri2", "default", "b")
    state = apply(
        state,
        [
            Command(command="start_flow", flow="promise_to_pay"),
            Command(command="start_flow", flow="dispute"),
        ],
    )
    reorder(state, FLOWS)

    assert state.flow_stack[-1].flow == "dispute"
    assert state.flow_stack[-1].parked is False
    assert state.flow_stack[0].flow == "promise_to_pay"
    assert state.flow_stack[0].parked is True


def test_priority_deterministic():
    state_a = new_conversation_state("c-det", "default", "b")
    state_b = new_conversation_state("c-det", "default", "b")
    cmds = [
        Command(command="start_flow", flow="pay_now"),
        Command(command="start_flow", flow="vulnerability"),
        Command(command="start_flow", flow="dispute"),
    ]
    state_a = apply(state_a, cmds)
    state_b = apply(state_b, cmds)
    reorder(state_a, FLOWS)
    reorder(state_b, FLOWS)

    assert [f.flow for f in state_a.flow_stack] == [f.flow for f in state_b.flow_stack]
    assert [f.parked for f in state_a.flow_stack] == [f.parked for f in state_b.flow_stack]
    assert state_a.flow_stack[-1].flow == "vulnerability"


def test_vulnerable_beats_dispute_and_ptp():
    state = new_conversation_state("c-vul", "default", "b")
    state = apply(
        state,
        [
            Command(command="start_flow", flow="promise_to_pay"),
            Command(command="start_flow", flow="dispute"),
            Command(command="start_flow", flow="vulnerability"),
        ],
    )
    reorder(state, FLOWS)

    assert state.flow_stack[-1].flow == "vulnerability"
    assert all(frame.parked for frame in state.flow_stack[:-1])


def test_identity_beats_dispute_and_ptp():
    state = new_conversation_state("c-id-pri", "default", "b")
    state = apply(
        state,
        [
            Command(command="start_flow", flow="promise_to_pay"),
            Command(command="start_flow", flow="dispute"),
            Command(command="start_flow", flow="identity_verification"),
        ],
    )
    reorder(state, FLOWS)

    assert state.flow_stack[-1].flow == "identity_verification"
    assert state.flow_stack[-1].parked is False
    assert all(frame.parked for frame in state.flow_stack[:-1])
