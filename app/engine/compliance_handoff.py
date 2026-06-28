"""Compliance & handoff flow helpers (FS-4)."""

from __future__ import annotations

from typing import Any

from app.schemas.state import BorrowerRecord, ConversationState

THIRD_PARTY_CONTACT_TYPES: frozenset[str] = frozenset(
    {
        "wrong_number",
        "reassigned_number",
        "family_member",
        "someone_else",
        "minor",
    }
)

DEBT_LEAKAGE_PHRASES: tuple[str, ...] = (
    "amount due",
    "due date",
    "arrears",
    "overdue",
    "outstanding",
    "dpd",
    "in arrears",
    "loan",
    "emi",
    "payment due",
    "borrower owes",
    "defaulter",
    "jama karna",
)

OPT_OUT_CHANNELS: frozenset[str] = frozenset({"voice", "sms", "whatsapp", "email", "all"})


def third_party_privacy_active(state: ConversationState) -> bool:
    from app.engine.identity_gate import third_party_privacy_active as _active

    return _active(state.slots)


def dunning_suppressed(state: ConversationState) -> bool:
    flags = state.slots.get("compliance_flags") or {}
    if isinstance(flags, dict) and flags.get("opt_out"):
        return True
    return bool(
        state.slots.get("dunning_suppressed")
        or flags.get("dunning_suppressed")
        or state.slots.get("harassment_complaint_logged")
        or state.slots.get("deceased_reported")
        or state.slots.get("incapacitated_reported")
    )


def reply_discloses_debt_or_arrears(text: str, state: ConversationState) -> bool:
    """Block debt/arrears language pre-identity or during third-party contact."""
    from app.engine.identity_gate import reply_discloses_debt

    return reply_discloses_debt(text, state)


def normalize_third_party_contact(value: Any) -> str | None:
    if value is None:
        return None
    token = str(value).lower().strip().replace(" ", "_").replace("-", "_")
    aliases = {
        "wrong_number": "wrong_number",
        "wrong": "wrong_number",
        "galat_number": "wrong_number",
        "reassigned": "reassigned_number",
        "family": "family_member",
        "relative": "family_member",
        "someone_else": "someone_else",
        "not_borrower": "someone_else",
        "minor": "minor",
        "child": "minor",
    }
    if token in THIRD_PARTY_CONTACT_TYPES:
        return token
    return aliases.get(token)


def classify_third_party_response(raw: Any) -> dict[str, bool]:
    text = str(raw or "").lower().strip()
    if not text:
        return {"third_party_minor": False, "third_party_not_borrower": False}
    if "minor" in text or "baccha" in text or "child" in text:
        return {"third_party_minor": True, "third_party_not_borrower": False}
    not_borrower_tokens = (
        "wrong",
        "galat",
        "not me",
        "not_borrower",
        "nahi",
        "no ",
        "someone else",
        "reassigned",
    )
    if any(token in text for token in not_borrower_tokens):
        return {"third_party_minor": False, "third_party_not_borrower": True}
    return {"third_party_minor": False, "third_party_not_borrower": False}


def sync_compliance_notes_on_persist(
    borrower: BorrowerRecord,
    state: ConversationState,
) -> BorrowerRecord:
    pending = state.slots.get("compliance_note_pending")
    if not pending:
        return borrower
    updated = borrower.model_copy(deep=True)
    notes = list(updated.notes)
    notes.append(dict(pending))
    updated.notes = notes
    return updated
