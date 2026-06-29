"""Hardcoded borrower for TEST_MODE bare-line calls (no DB / Redis lookup)."""

from __future__ import annotations

from datetime import date, timedelta

from app.schemas.state import BorrowerRecord, ConversationState

_SOT_LOAN_KEYS: tuple[str, ...] = (
    "customer_name",
    "repay_amount",
    "offer_amount",
    "discount_amount",
    "loan_amount",
    "loan_tenure",
    "due_date",
    "disbursal_date",
    "amount_due",
)


def hardcoded_test_borrower(borrower_id: str = "sot_test_borrower") -> BorrowerRecord:
    """Fixed pre_closure scenario borrower for salary_on_time test calls."""
    today = date.today()
    due = today + timedelta(days=5)
    disbursal = today - timedelta(days=25)
    return BorrowerRecord(
        borrower_id=borrower_id,
        identity={"name": "Rishabh"},
        loan={
            "customer_name": "Rishabh",
            "repay_amount": 2300,
            "offer_amount": 2000,
            "discount_amount": 300,
            "loan_amount": 2020,
            "loan_tenure": "4 months",
            "due_date": due.isoformat(),
            "disbursal_date": disbursal.isoformat(),
            "amount_due": 2300,
        },
    )


def apply_test_borrower_slots(
    state: ConversationState,
    borrower: BorrowerRecord,
) -> ConversationState:
    """Surface SOT loan fields as conversation slots for templated NLG."""
    updated = state.model_copy(deep=True)
    slots = dict(updated.slots)
    loan = borrower.loan or {}
    for key in _SOT_LOAN_KEYS:
        if key in loan:
            slots[key] = loan[key]
    name = borrower.identity.get("name") or loan.get("customer_name")
    if name:
        slots["borrower_name"] = name
        slots.setdefault("customer_name", name)
    updated.slots = slots
    return updated
