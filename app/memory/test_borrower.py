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

_PLO_LOAN_KEYS: tuple[str, ...] = (
    "customer_name",
    "repay_amount",
    "loan_amount",
    "due_date",
    "disbursal_date",
    "amount_due",
    "days_past_due",
    "dpd",
    "branch",
    "branch_address",
    "last_date_paid",
    "product",
    "npa_flag",
)

# days_past_due used by select_plo_scenario when TEST_PLO_SCENARIO is unset.
_PLO_SCENARIO_DPD: dict[str, int] = {
    "predue": -5,
    "ondue": 0,
    "postdue1": 15,
    "postdue2": 45,
    "postdue3": 75,
    "npa": 120,
}


def _scenario_due_date(scenario: str, today: date) -> date:
    """Map the TEST_SOT_SCENARIO flag to the test borrower's due_date.

    pre -> future (discount still live), on_due -> today, post_due -> already
    overdue (penalty accruing). select_sot_scenario then derives the script.
    """
    key = (scenario or "pre").strip().lower()
    if key in {"on_due", "on-due", "ondue", "on"}:
        return today
    if key in {"post_due", "post-due", "postdue", "post"}:
        return today - timedelta(days=4)
    return today + timedelta(days=5)


def hardcoded_test_borrower(
    borrower_id: str = "sot_test_borrower",
    scenario: str | None = None,
) -> BorrowerRecord:
    """Hardcoded salary_on_time test borrower.

    The due_date (and therefore which sub-script runs) follows the requested
    scenario, defaulting to TEST_SOT_SCENARIO from settings. On/Post-due carry no
    live discount, so offer_amount == repay_amount and discount is zero — this
    keeps the shared sot_commit confirm lines correct without per-scenario amounts.
    """
    if scenario is None:
        # Imported lazily to avoid a config import cycle at module load.
        from app.config import get_settings

        scenario = get_settings().test_sot_scenario
    today = date.today()
    due = _scenario_due_date(scenario, today)
    disbursal = today - timedelta(days=25)
    is_pre = due > today
    repay = 2300
    return BorrowerRecord(
        borrower_id=borrower_id,
        identity={"name": "Rishabh"},
        loan={
            "customer_name": "Rishabh",
            "repay_amount": repay,
            "offer_amount": 2000 if is_pre else repay,
            "discount_amount": 300 if is_pre else 0,
            "loan_amount": 2020,
            "loan_tenure": "4 months",
            "due_date": due.isoformat(),
            "disbursal_date": disbursal.isoformat(),
            "amount_due": repay,
        },
    )


def hardcoded_paisalo_borrower(
    borrower_id: str = "plo_test_borrower",
    scenario: str | None = None,
) -> BorrowerRecord:
    """Hardcoded PaisaLo test borrower for goldens / TEST_MODE."""
    if scenario is None:
        from app.config import get_settings

        scenario = get_settings().test_plo_scenario
    key = (scenario or "postdue1").strip().lower()
    today = date.today()
    dpd = _PLO_SCENARIO_DPD.get(key, 15)
    due = today - timedelta(days=dpd) if dpd >= 0 else today + timedelta(days=abs(dpd))
    disbursal = today - timedelta(days=180)
    last_paid = today - timedelta(days=max(dpd, 0) + 30)
    repay = 4500
    return BorrowerRecord(
        borrower_id=borrower_id,
        identity={"name": "रमेश"},
        loan={
            "customer_name": "रमेश",
            "repay_amount": repay,
            "loan_amount": 50000,
            "due_date": due.isoformat(),
            "disbursal_date": disbursal.isoformat(),
            "amount_due": repay,
            "days_past_due": dpd,
            "dpd": dpd,
            "branch": "कानपुर सिटी",
            "branch_address": "12 एमजी रोड, कानपुर",
            "last_date_paid": last_paid.isoformat(),
            "product": "ABF",
            "npa_flag": key == "npa",
        },
    )


def apply_test_borrower_slots(
    state: ConversationState,
    borrower: BorrowerRecord,
) -> ConversationState:
    """Surface loan fields as conversation slots for templated NLG."""
    updated = state.model_copy(deep=True)
    slots = dict(updated.slots)
    loan = borrower.loan or {}
    keys = _PLO_LOAN_KEYS if state.tenant_id == "paisalo" else _SOT_LOAN_KEYS
    for key in keys:
        if key in loan:
            slots[key] = loan[key]
    # Always surface common keys present on the loan dict.
    for key, value in loan.items():
        slots.setdefault(key, value)
    name = borrower.identity.get("name") or loan.get("customer_name")
    if name:
        slots["borrower_name"] = name
        slots.setdefault("customer_name", name)
    if state.tenant_id == "paisalo":
        # Force scenario for goldens even when dpd would disagree.
        from app.config import get_settings

        override = (get_settings().test_plo_scenario or "").strip().lower()
        if override:
            slots["plo_scenario_override"] = override
    updated.slots = slots
    return updated
