"""Dispute breadth helpers (FS-8) — verify-then-act, dispute-hold discipline."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from app.schemas.state import BorrowerRecord, ConversationState

DISPUTE_DISPOSITIONS: dict[str, str] = {
    "amount": "DISPUTE_AMOUNT",
    "loan_closed": "DISPUTE_CLOSED",
    "not_due_yet": "DISPUTE_NOT_DUE",
    "nach": "DISPUTE_NACH",
    "double_charge": "DOUBLE_CHARGE_REVIEW",
    "prior_payment": "DISPUTE",
}


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
    return parsed if parsed is not None else date.today()


def _parse_amount(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def apply_dispute_hold_slots(slots: dict[str, Any]) -> dict[str, Any]:
    flags = dict(slots.get("compliance_flags") or {})
    flags["dispute_hold"] = True
    slots["compliance_flags"] = flags
    slots["pressure_allowed"] = False
    slots["dispute_active"] = True
    return slots


def apply_amount_verification(slots: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    record_due = _parse_amount(result.get("amount_due"))
    claimed = _parse_amount(slots.get("dispute_claim"))
    claim_text = str(slots.get("dispute_claim") or "").lower()
    charges = _parse_amount(result.get("charges")) or 0.0

    if record_due is not None:
        slots["amount_on_record"] = record_due
        slots["amount_due"] = record_due

    if claimed is not None and record_due is not None and abs(claimed - record_due) < 0.01:
        slots["amount_borrower_correct"] = True
        slots["amount_route_billing"] = False
    elif "charge" in claim_text and charges > 0:
        slots["amount_borrower_correct"] = True
        slots["amount_route_billing"] = True
    elif claimed is not None and record_due is not None and claimed < record_due:
        slots["amount_borrower_correct"] = False
        slots["amount_route_billing"] = True
    else:
        slots["amount_borrower_correct"] = False
        slots["amount_route_billing"] = False

    slots["amount_verified"] = True
    return slots


def apply_loan_status_verification(slots: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    status = str(result.get("loan_status") or "active").lower()
    amount_due = _parse_amount(result.get("amount_due")) or 0.0
    slots["loan_is_closed"] = status == "closed" or (status != "active" and amount_due <= 0)
    slots["loan_status_verified"] = True
    if result.get("amount_due") is not None:
        slots["amount_due"] = result.get("amount_due")
    return slots


def apply_not_due_verification(slots: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    due = _parse_date(result.get("due_date"))
    today = _call_today_from_slots(slots)
    slots["not_due_borrower_right"] = bool(due and due > today)
    slots["not_due_verified"] = True
    if due:
        slots["due_date"] = due.isoformat()
    return slots


def _call_today_from_slots(slots: dict[str, Any]) -> date:
    raw = slots.get("call_date") or slots.get("today")
    parsed = _parse_date(raw)
    return parsed if parsed is not None else date.today()


def apply_nach_verification(slots: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    side = str(result.get("nach_failure_side") or result.get("failure_side") or "").lower()
    status = str(result.get("nach_status") or result.get("status") or "").lower()
    slots["nach_lender_fault"] = side == "lender" or "lender" in status
    slots["nach_verified"] = True
    if result.get("found"):
        slots["payment_found"] = True
        slots["payment_status"] = status or result.get("status")
    return slots


def build_dispute_record(state: ConversationState, *, disposition: str) -> dict[str, Any]:
    return {
        "type": "dispute",
        "dispute_type": state.slots.get("dispute_type"),
        "claim": state.slots.get("dispute_claim") or state.slots.get("dispute_reason") or "",
        "disposition": disposition,
        "ts": datetime.now(UTC).isoformat(),
    }


def sync_dispute_on_persist(
    borrower: BorrowerRecord,
    state: ConversationState,
) -> BorrowerRecord:
    updated = borrower.model_copy(deep=True)

    record = state.slots.get("dispute_record_pending")
    if record:
        disputes = list(updated.disputes)
        disputes.append(dict(record))
        updated.disputes = disputes

    flags = dict(updated.compliance_flags)
    state_flags = state.slots.get("compliance_flags") or {}
    if state_flags.get("dispute_hold"):
        flags["dispute_hold"] = True
    updated.compliance_flags = flags

    return updated
