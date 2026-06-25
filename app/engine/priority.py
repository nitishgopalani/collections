"""Priority resolver for multi-signal flow ordering (Sprint 5)."""

from app.schemas.flow import FlowSet
from app.schemas.state import ConversationState

# Higher index = lower priority. opt_out wins over everything.
PRIORITY_LADDER: tuple[str, ...] = (
    "opt_out",
    "vulnerable",
    "identity",
    "dispute",
    "hardship",
    "ptp",
    "refusal",
    "reminder",
)

_DEFAULT_PRIORITY = "reminder"


def priority_key(flow_name: str, flows: FlowSet) -> str:
    flow = flows.flows.get(flow_name)
    if flow is None:
        return _DEFAULT_PRIORITY
    return flow.priority


def priority_rank(flow_name: str, flows: FlowSet) -> int:
    key = priority_key(flow_name, flows)
    try:
        return PRIORITY_LADDER.index(key)
    except ValueError:
        return len(PRIORITY_LADDER)


def reorder(state: ConversationState, flows: FlowSet) -> ConversationState:
    """Sort flow_stack by priority ladder; highest-priority frame active, rest parked."""
    stack = state.flow_stack
    if not stack:
        return state

    if len(stack) == 1:
        stack[0].parked = False
        return state

    ordered = sorted(
        stack,
        key=lambda frame: priority_rank(frame.flow, flows),
        reverse=True,
    )
    for index, frame in enumerate(ordered):
        frame.parked = index < len(ordered) - 1
    state.flow_stack = ordered
    return state
