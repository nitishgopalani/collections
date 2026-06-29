"""Identity entry gate — verification before collection disclosure (FS-1)."""

from __future__ import annotations

import re
from typing import Any

from app.schemas.flow import FlowSet
from app.schemas.state import ConversationState, Frame

COLLECTION_FLOWS: frozenset[str] = frozenset(
    {
        "pay_now",
        "promise_to_pay",
        "dispute",
        "partial_payment",
        "already_initiated",
        "dues_breakup",
        "alt_channel",
        "hardship",
        "vague_ptp",
        "balance_inquiry",
        "due_date_inquiry",
        "loan_terms_inquiry",
        "payment_link_nudge",
        "ptp_followup",
    }
)
IDENTITY_FLOWS: frozenset[str] = frozenset(
    {
        "identity_verification",
        "identity_refused",
        "who_are_you",
        "bot_disclosure",
        "recording_disclosure",
        "repeat_request",
        "human_handoff_request",
        "simple_ptp_test",
        "identity_name_confirm",
    }
)

DEBT_SLOT_KEYS: frozenset[str] = frozenset(
    {
        "amount_due",
        "dpd",
        "bucket",
        "outstanding",
        "last_payment_amount",
        "last_payment_date",
        "principal",
        "interest",
        "charges",
        "balance_remaining",
        "partial_amount",
    }
)

DEBT_DISCLOSURE_PHRASES: tuple[str, ...] = (
    "amount due",
    "due date",
    "arrears",
    "overdue",
    "outstanding",
    "dpd",
    "days past due",
)

THIRD_PARTY_DEBT_PHRASES: tuple[str, ...] = (
    "in arrears",
    "loan",
    "emi",
    "payment due",
    "borrower owes",
    "defaulter",
    "jama karna",
)

COMPLIANCE_HANDOFF_FLOWS: frozenset[str] = frozenset(
    {
        "opt_out",
        "third_party",
        "fraud_claim",
        "lawyer_rep",
        "deceased_borrower",
        "incapacitated_borrower",
        "harassment_complaint",
    }
)


def identity_ok(state: ConversationState) -> bool:
    return bool(state.slots.get("identity_ok"))


def third_party_privacy_active(slots: dict[str, Any]) -> bool:
    return bool(
        slots.get("third_party_active")
        or slots.get("confirmed_not_borrower")
        or slots.get("third_party_minor")
    )


def must_block_debt_disclosure(slots: dict[str, Any]) -> bool:
    if third_party_privacy_active(slots):
        return True
    return not bool(slots.get("identity_ok"))


def apply_identity_entry_gate(
    state: ConversationState,
    flows: FlowSet | None = None,
) -> ConversationState:
    """Ensure identity verification runs before collection when not yet verified."""
    _ = flows
    if state.slots.get("_force_test_flow") in {"simple_ptp_test", "identity_name_confirm"}:
        return state
    if identity_ok(state) or third_party_privacy_active(state.slots):
        return state

    updated = state.model_copy(deep=True)
    raw_name = str(updated.slots.get("borrower_name") or "").strip()
    if not raw_name or raw_name.lower() in {"unknown", "none", "null"}:
        # No resolved borrower name — fall back to a polite generic right-party greeting.
        updated.slots["borrower_name"] = "aap"
    stack_names = {frame.flow for frame in updated.flow_stack}
    if "identity_verification" in stack_names:
        return updated

    updated.flow_stack.insert(0, Frame(flow="identity_verification", step_index=0))
    return updated


def defer_collection_flows(state: ConversationState, flows: FlowSet) -> ConversationState:
    """Park collection flows until identity_ok; identity frame stays active."""
    if identity_ok(state) or not state.flow_stack:
        return state

    updated = state.model_copy(deep=True)
    stack_names = {frame.flow for frame in updated.flow_stack}
    if not stack_names & COLLECTION_FLOWS:
        return updated

    if "identity_verification" not in stack_names:
        updated.flow_stack.insert(0, Frame(flow="identity_verification", step_index=0))
        stack_names.add("identity_verification")

    for frame in updated.flow_stack:
        if frame.flow in COLLECTION_FLOWS:
            frame.parked = True
        elif frame.flow in IDENTITY_FLOWS:
            frame.parked = False

    return updated


def slots_for_nlg(slots: dict[str, Any]) -> dict[str, Any]:
    """Strip debt fields from NLG slots when identity or third-party privacy requires it."""
    if not must_block_debt_disclosure(slots):
        return slots
    sanitized = dict(slots)
    for key in DEBT_SLOT_KEYS:
        sanitized.pop(key, None)
    return sanitized


def template_references_debt(template: str) -> bool:
    for key in DEBT_SLOT_KEYS:
        if f"{{{key}}}" in template:
            return True
    return False


def reply_discloses_debt(text: str, state: ConversationState) -> bool:
    """Detect outbound debt detail before identity verification or to third parties."""
    if not must_block_debt_disclosure(state.slots):
        return False

    normalized = re.sub(r"\s+", " ", text.lower().strip())
    slots = state.slots

    for phrase in DEBT_DISCLOSURE_PHRASES:
        if phrase in normalized:
            return True

    if third_party_privacy_active(slots):
        for phrase in THIRD_PARTY_DEBT_PHRASES:
            if phrase in normalized:
                return True

    for key in DEBT_SLOT_KEYS:
        value = slots.get(key)
        if value is None:
            continue
        token = str(value).lower().strip()
        if token and token in normalized:
            return True
        if key == "amount_due" and isinstance(value, (int, float)):
            amount = int(value)
            if str(amount) in normalized.replace(",", ""):
                return True

    return False
