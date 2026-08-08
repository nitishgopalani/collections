"""Unit tests for local Postgres borrower mapping (no live DB required)."""

from decimal import Decimal

from app.memory.postgres_borrowers import normalize_phone, row_to_borrower


def test_normalize_phone_e164():
    assert normalize_phone("+91 98105 87857") == "+919810587857"
    assert normalize_phone("9810587857") == "+919810587857"
    assert normalize_phone("919810587857") == "+919810587857"


def test_row_to_borrower_maps_fields():
    record = row_to_borrower(
        {
            "id": "B_RAJESH",
            "name": "Rajesh",
            "phone": "+919876543210",
            "amount_due": Decimal("350"),
            "account_ref": "LN-1",
            "language": "hi-IN",
            "tenant_id": "default",
        }
    )
    assert record.borrower_id == "B_RAJESH"
    assert record.identity["name"] == "Rajesh"
    assert record.loan["amount_due"] == 350
    assert record.comms_prefs["phone"] == "+919876543210"


def test_row_to_borrower_maps_paisalo_loan_fields():
    record = row_to_borrower(
        {
            "id": "PLO_RAMESH_PREDUE",
            "name": "Ramesh",
            "phone": "+919810587857",
            "amount_due": Decimal("4500"),
            "account_ref": "PLO-ABF-RM-001",
            "language": "hi-IN",
            "tenant_id": "paisalo",
            "repay_amount": Decimal("4500"),
            "loan_amount": Decimal("50000"),
            "due_date": "2026-08-13",
            "disbursal_date": "2026-02-09",
            "days_past_due": -5,
            "dpd": -5,
            "branch": "Kanpur City",
            "branch_address": "12 MG Road, Kanpur",
            "last_date_paid": "2026-07-13",
            "product": "ABF",
            "npa_flag": False,
        }
    )
    loan = record.loan
    assert loan["amount_due"] == 4500
    assert loan["repay_amount"] == 4500
    assert loan["loan_amount"] == 50000
    assert loan["due_date"] == "2026-08-13"
    assert loan["disbursal_date"] == "2026-02-09"
    assert loan["days_past_due"] == -5
    assert loan["dpd"] == -5
    assert loan["branch"] == "Kanpur City"
    assert loan["branch_address"] == "12 MG Road, Kanpur"
    assert loan["last_date_paid"] == "2026-07-13"
    assert loan["product"] == "ABF"
    assert loan["npa_flag"] is False


def test_row_to_borrower_omits_null_paisalo_fields():
    record = row_to_borrower(
        {
            "id": "B_RAJESH",
            "name": "Rajesh",
            "phone": "+919876543210",
            "amount_due": Decimal("350"),
            "account_ref": "LN-1",
            "language": "hi-IN",
            "tenant_id": "default",
            "repay_amount": None,
            "loan_amount": None,
            "due_date": None,
            "disbursal_date": None,
            "days_past_due": None,
            "dpd": None,
            "branch": None,
            "branch_address": None,
            "last_date_paid": None,
            "product": None,
            "npa_flag": None,
        }
    )
    loan = record.loan
    assert "repay_amount" not in loan
    assert "dpd" not in loan
    assert "branch" not in loan
    assert "npa_flag" not in loan
    assert loan["amount_due"] == 350
