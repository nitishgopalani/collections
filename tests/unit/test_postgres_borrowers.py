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
