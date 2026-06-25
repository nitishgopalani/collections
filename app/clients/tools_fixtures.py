"""Borrower fixture data for the simulated tool backend."""

from typing import Any

B_PAID = "B_PAID"
B_DUE = "B_DUE"
B_PARTIAL = "B_PARTIAL"
B_VULNERABLE = "B_VULNERABLE"
B_VERIFY_OK = "B_VERIFY_OK"
B_VERIFY_FAIL = "B_VERIFY_FAIL"

BORROWER_FIXTURES: dict[str, dict[str, Any]] = {
    B_PAID: {
        "loan_id": "LN-PAID-001",
        "amount_due": 0,
        "dpd": 0,
        "bucket": "current",
        "payments": [
            {
                "payment_id": "PAY-1001",
                "amount": 5000,
                "date": "2026-06-20",
                "status": "posted",
            }
        ],
        "dispute_open": False,
        "followup_scheduled": False,
        "disposition": None,
        "payment_links": [],
        "simulate_errors": False,
    },
    B_DUE: {
        "loan_id": "LN-DUE-002",
        "amount_due": 5000,
        "dpd": 45,
        "bucket": "30-60",
        "payments": [],
        "dispute_open": False,
        "followup_scheduled": False,
        "disposition": None,
        "payment_links": [],
        "simulate_errors": False,
    },
    B_PARTIAL: {
        "loan_id": "LN-PART-003",
        "amount_due": 2500,
        "dpd": 15,
        "bucket": "0-30",
        "payments": [
            {
                "payment_id": "PAY-2001",
                "amount": 2500,
                "date": "2026-06-10",
                "status": "posted",
            }
        ],
        "dispute_open": False,
        "followup_scheduled": False,
        "disposition": None,
        "payment_links": [],
        "simulate_errors": False,
    },
    B_VULNERABLE: {
        "loan_id": "LN-VUL-004",
        "amount_due": 8000,
        "dpd": 60,
        "bucket": "60-90",
        "payments": [],
        "dispute_open": False,
        "followup_scheduled": False,
        "disposition": None,
        "payment_links": [],
        "simulate_errors": True,
        "vulnerable": True,
    },
    B_VERIFY_OK: {
        "loan_id": "LN-VERIFY-005",
        "amount_due": 5000,
        "dpd": 30,
        "bucket": "0-30",
        "payments": [],
        "dispute_open": False,
        "followup_scheduled": False,
        "disposition": None,
        "payment_links": [],
        "simulate_errors": False,
        "identity": {
            "name": "Raj Kumar",
            "dob": "1985-03-15",
            "last4": "4321",
        },
    },
    B_VERIFY_FAIL: {
        "loan_id": "LN-VERIFY-006",
        "amount_due": 5000,
        "dpd": 30,
        "bucket": "0-30",
        "payments": [],
        "dispute_open": False,
        "followup_scheduled": False,
        "disposition": None,
        "payment_links": [],
        "simulate_errors": False,
        "identity": {
            "name": "Priya Sharma",
            "dob": "1990-07-22",
            "last4": "9876",
        },
    },
}
