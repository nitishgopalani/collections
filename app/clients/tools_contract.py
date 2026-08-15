"""Shared TOOLS_API_CONTRACT helpers — two endpoints, tenant-agnostic."""

from __future__ import annotations

from typing import Any

from app.schemas.state import BorrowerRecord

BORROWER_STATE_PATH = "/v1/borrower_state"
DISPOSITION_PATH = "/v1/disposition"

STATE_TOOLS = frozenset(
    {
        "get_borrower_state",
        "get_borrower",
        "check_last_payment",
        "get_balance",
    }
)
DISPOSITION_TOOLS = frozenset({"post_disposition", "log_disposition"})
STUB_FORBIDDEN = frozenset(
    {
        "hangup_call",
        "hangup",
        "end_call",
        "transfer_call",
        "send_payment_link",
        "create_payment_link",
    }
)


def borrower_state_from_record(record: BorrowerRecord) -> dict[str, Any]:
    """Map a hydrated borrower row to the contract payload."""
    loan = record.loan or {}
    outstanding = loan.get("outstanding")
    if outstanding is None:
        outstanding = loan.get("amount_due")
    if outstanding is None:
        outstanding = loan.get("repay_amount")

    last_payment: dict[str, Any] | None = None
    payments = record.payments or []
    if payments:
        last = payments[-1]
        last_payment = {
            "date": last.get("date") or last.get("paid_on"),
            "amount": last.get("amount"),
        }
    elif loan.get("last_date_paid"):
        last_payment = {
            "date": loan.get("last_date_paid"),
            "amount": loan.get("last_payment_amount"),
        }

    ptp_on_file: dict[str, Any] | None = None
    ptps = record.ptps or []
    if ptps:
        last_ptp = ptps[-1]
        ptp_on_file = {
            "date": last_ptp.get("date") or last_ptp.get("ptp_date"),
            "amount": last_ptp.get("amount") or last_ptp.get("ptp_amount"),
        }
    elif loan.get("committed_date") or loan.get("ptp_date"):
        ptp_on_file = {
            "date": loan.get("committed_date") or loan.get("ptp_date"),
            "amount": loan.get("ptp_amount") or loan.get("repay_amount"),
        }

    return {
        "outstanding": outstanding,
        "last_payment": last_payment,
        "ptp_on_file": ptp_on_file,
        "borrower_id": record.borrower_id,
        "loan_ref": loan.get("account_ref"),
        "phone": (record.comms_prefs or {}).get("phone"),
    }


def apply_borrower_state(
    record: BorrowerRecord,
    result: dict[str, Any],
) -> BorrowerRecord:
    """Overlay contract fields onto a BorrowerRecord (snapshot stay intact if absent)."""
    if not result:
        return record
    updated = record.model_copy(deep=True)
    loan = dict(updated.loan or {})
    if result.get("outstanding") is not None:
        loan["amount_due"] = result["outstanding"]
        loan["outstanding"] = result["outstanding"]
    last = result.get("last_payment")
    if isinstance(last, dict):
        if last.get("date"):
            loan["last_date_paid"] = last["date"]
        if last.get("amount") is not None:
            loan["last_payment_amount"] = last["amount"]
        updated.payments = [
            *(updated.payments or []),
            {"date": last.get("date"), "amount": last.get("amount")},
        ]
    ptp = result.get("ptp_on_file")
    if isinstance(ptp, dict) and (ptp.get("date") or ptp.get("amount") is not None):
        loan["committed_date"] = ptp.get("date")
        loan["ptp_date"] = ptp.get("date")
        if ptp.get("amount") is not None:
            loan["ptp_amount"] = ptp["amount"]
        updated.ptps = [
            *(updated.ptps or []),
            {"date": ptp.get("date"), "amount": ptp.get("amount")},
        ]
    if result.get("loan_ref"):
        loan["account_ref"] = result["loan_ref"]
    updated.loan = loan
    if result.get("phone"):
        comms = dict(updated.comms_prefs or {})
        comms["phone"] = result["phone"]
        updated.comms_prefs = comms
    if result.get("borrower_id"):
        updated.borrower_id = str(result["borrower_id"])
    return updated


def unwrap_state_payload(response: dict[str, Any]) -> dict[str, Any]:
    """Accept either a raw contract body or {ok, result}."""
    if not response:
        return {}
    inner = response.get("result")
    if isinstance(inner, dict) and (
        "outstanding" in inner or "last_payment" in inner or "ptp_on_file" in inner
    ):
        return inner
    if "outstanding" in response or "last_payment" in response or "ptp_on_file" in response:
        return response
    return inner if isinstance(inner, dict) else {}
