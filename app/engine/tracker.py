from datetime import UTC, datetime

from app.schemas.command import Command
from app.schemas.state import BorrowerRecord, ConversationState, Event, Frame, Frame

CommandOrEvent = Command | Event

_HYDRATION_LOAN_KEYS = (
    "amount_due",
    "dpd",
    "bucket",
    # PaisaLo / shared collection facts (P5).
    "days_past_due",
    "branch",
    "branch_address",
    "last_date_paid",
    "product",
    "npa_flag",
    "repay_amount",
    "loan_amount",
    "disbursal_date",
    "due_date",
    "customer_name",
    # G-B4-02: committed_date is hydrated from DB (prior-call commitment) AND
    # written back during the assurance-date flow (see coerce_committed_date).
    "committed_date",
)


def new_conversation_state(
    call_id: str,
    tenant_id: str,
    borrower_id: str,
) -> ConversationState:
    return ConversationState(
        call_id=call_id,
        tenant_id=tenant_id,
        borrower_id=borrower_id,
    )


def hydrate_from_borrower(
    state: ConversationState,
    borrower: BorrowerRecord | None,
) -> ConversationState:
    """Populate live call slots from durable borrower memory at turn start."""
    if borrower is None:
        return state
    hydrated = state.model_copy(deep=True)
    slots = dict(hydrated.slots)
    loan = borrower.loan
    for key in _HYDRATION_LOAN_KEYS:
        if key in loan:
            slots[key] = loan[key]
    if "amount_due" not in slots and "outstanding" in loan:
        slots["amount_due"] = loan["outstanding"]
    slots["compliance_flags"] = dict(borrower.compliance_flags)
    slots["trust"] = borrower.trust_current
    slots["risk_flags"] = list(borrower.risk_flags)
    slots["persona"] = dict(borrower.persona_current) if borrower.persona_current else {}
    if borrower.emotions:
        last = borrower.emotions[-1]
        slots["emotion"] = last.get("emotion") or last.get("label")
        slots["emotion_intensity"] = last.get("intensity", "med")
        if last.get("tone_register"):
            slots["tone_register"] = last["tone_register"]
    if borrower.recovery:
        slots["recovery"] = dict(borrower.recovery)
    if borrower.identity.get("identity_ok"):
        slots["identity_ok"] = True
    name = borrower.identity.get("name")
    if name:
        slots["borrower_name"] = name
    comms = borrower.comms_prefs or {}
    phone = comms.get("phone") or comms.get("whatsapp")
    if phone:
        slots["borrower_phone"] = phone
        slots["phone"] = phone
    hydrated.slots = slots
    return hydrated


def apply(state: ConversationState, items: list[CommandOrEvent]) -> ConversationState:
    """Pure state transition: commands/events applied, events appended, version bumped."""
    if not items:
        return state
    updated = state.model_copy(deep=True)
    for item in items:
        if isinstance(item, Event):
            updated.events.append(item)
        else:
            updated = _apply_command(updated, item)
    updated.version += 1
    return updated


def _apply_command(state: ConversationState, command: Command) -> ConversationState:
    ts = datetime.now(UTC).isoformat()
    event_data = command.model_dump(mode="json")

    if command.command == "start_flow":
        if command.flow:
            state.flow_stack.append(Frame(flow=command.flow))
    elif command.command == "set_slot":
        if command.name is not None:
            state.slots[command.name] = command.value
    elif command.command == "cancel_flow":
        if command.flow:
            state.flow_stack = [frame for frame in state.flow_stack if frame.flow != command.flow]
        elif state.flow_stack:
            state.flow_stack.pop()
    elif command.command == "human_handoff":
        state.slots["transfer_to_human"] = True
        state.slots["human_handoff_requested"] = True
    elif command.command == "cannot_handle":
        state.slots["needs_clarify"] = True

    state.events.append(
        Event(
            ts=ts,
            kind="command",
            data=event_data,
            rationale=command.reason,
        )
    )
    return state
