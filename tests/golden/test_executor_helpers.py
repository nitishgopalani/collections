from app.engine.priority import reorder
from app.engine.tracker import apply, new_conversation_state
from app.flows.loader import load_all_flows
from app.schemas.command import Command

FLOWS = load_all_flows()
CALL_DATE = "2026-06-25"


def _base_state(flow: str, borrower_id: str = "borrower-golden"):
    state = new_conversation_state("call-golden", "default", borrower_id)
    state.slots["call_date"] = CALL_DATE
    state.slots["amount_due"] = 5000
    return apply(state, [Command(command="start_flow", flow=flow)])


def _stack_dispute_over_promise(borrower_id: str):
    state = new_conversation_state("call-dispute-ptp", "default", borrower_id)
    state.slots["call_date"] = CALL_DATE
    state.slots["amount_due"] = 5000
    state = apply(
        state,
        [
            Command(command="start_flow", flow="promise_to_pay"),
            Command(command="start_flow", flow="dispute"),
        ],
    )
    reorder(state, FLOWS)
    return state
