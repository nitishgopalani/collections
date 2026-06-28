"""Per-call borrower variables from session_start / campaign upload metadata."""

from __future__ import annotations

from typing import Any

from app.schemas.state import BorrowerRecord, ConversationState
from app.util.phone import canonical_phone

BORROWER_CONTEXT_SLOT_KEYS: frozenset[str] = frozenset(
    {
        "borrower_name",
        "amount_due",
        "borrower_phone",
        "phone",
        "account_ref",
        "language",
    }
)


def normalize_borrower_context(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not raw:
        return {}
    normalized: dict[str, Any] = {}
    for key in ("borrower_name", "account_ref", "language"):
        value = raw.get(key)
        if value is not None and str(value).strip():
            normalized[key] = str(value).strip()
    phone = raw.get("phone") or raw.get("borrower_phone") or raw.get("customer_phone")
    if phone is not None and str(phone).strip():
        canonical = canonical_phone(str(phone).strip()) or str(phone).strip()
        normalized["phone"] = canonical
        normalized["borrower_phone"] = canonical
    amount = raw.get("amount_due")
    if amount is not None:
        try:
            normalized["amount_due"] = int(amount) if float(amount).is_integer() else float(amount)
        except (TypeError, ValueError):
            normalized["amount_due"] = amount
    return normalized


def apply_borrower_context_to_record(
    borrower: BorrowerRecord,
    context: dict[str, Any],
) -> BorrowerRecord:
    ctx = normalize_borrower_context(context)
    if not ctx:
        return borrower
    updated = borrower.model_copy(deep=True)
    if "borrower_name" in ctx:
        identity = dict(updated.identity)
        identity["name"] = ctx["borrower_name"]
        updated.identity = identity
    if "amount_due" in ctx:
        loan = dict(updated.loan)
        loan["amount_due"] = ctx["amount_due"]
        updated.loan = loan
    if "phone" in ctx:
        comms = dict(updated.comms_prefs)
        comms["phone"] = ctx["phone"]
        comms["whatsapp"] = ctx["phone"]
        updated.comms_prefs = comms
    if "account_ref" in ctx:
        loan = dict(updated.loan)
        loan["account_ref"] = ctx["account_ref"]
        updated.loan = loan
    if "language" in ctx:
        comms = dict(updated.comms_prefs)
        comms["language"] = ctx["language"]
        updated.comms_prefs = comms
    return updated


def apply_borrower_context_to_state(
    state: ConversationState,
    context: dict[str, Any],
) -> ConversationState:
    ctx = normalize_borrower_context(context)
    if not ctx:
        return state
    updated = state.model_copy(deep=True)
    slots = dict(updated.slots)
    if "borrower_name" in ctx:
        slots["borrower_name"] = ctx["borrower_name"]
    if "amount_due" in ctx:
        slots["amount_due"] = ctx["amount_due"]
    if "phone" in ctx:
        slots["borrower_phone"] = ctx["phone"]
        slots["phone"] = ctx["phone"]
    if "account_ref" in ctx:
        slots["account_ref"] = ctx["account_ref"]
    if "language" in ctx:
        slots["language"] = ctx["language"]
    updated.slots = slots
    return updated
