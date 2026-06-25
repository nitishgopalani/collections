"""Sprint 2 placeholder — superseded by app.engine.actions (Sprint 3)."""

from datetime import date, datetime
from typing import Any

from app.schemas.state import ConversationState


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    return None


def _call_today(state: ConversationState) -> date:
    raw = state.slots.get("call_date") or state.slots.get("today")
    parsed = _parse_date(raw)
    if parsed is not None:
        return parsed
    return date.today()


def placeholder_action_runner(action: str, state: ConversationState) -> ConversationState:
    updated = state.model_copy(deep=True)
    slots = dict(updated.slots)

    if action == "validate_ptp":
        today = _call_today(updated)
        ptp_date = _parse_date(slots.get("ptp_date"))
        if ptp_date is None:
            slots["ptp_allowed"] = False
        else:
            days_out = (ptp_date - today).days
            # DECISION NEEDED: confirm max PTP horizon with product/compliance.
            slots["ptp_allowed"] = 0 <= days_out <= 14
        updated.slots = slots
        return updated

    if action == "schedule_followup":
        slots["followup_scheduled"] = True
        updated.slots = slots
        return updated

    if action == "raise_dispute_ticket":
        slots["dispute_logged"] = bool(slots.get("dispute_reason"))
        updated.slots = slots
        return updated

    if action == "route_vulnerable":
        slots["transfer_to_human"] = True
        slots["vulnerable_routed"] = True
        updated.slots = slots
        return updated

    if action == "create_payment_link":
        slots["payment_link"] = "https://pay.example/link-stub"
        updated.slots = slots
        return updated

    raise KeyError(f"Unknown placeholder action: {action}")
