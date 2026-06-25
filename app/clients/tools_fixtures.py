"""Borrower fixture data for the simulated tool backend."""

from typing import Any

B_PAID = "B_PAID"
B_DUE = "B_DUE"
B_PARTIAL = "B_PARTIAL"
B_VULNERABLE = "B_VULNERABLE"
B_VERIFY_OK = "B_VERIFY_OK"
B_VERIFY_FAIL = "B_VERIFY_FAIL"
B_PROCESSING = "B_PROCESSING"
B_CLOSED = "B_CLOSED"
B_OVERDUE = "B_OVERDUE"
B_NACH_LENDER = "B_NACH_LENDER"
B_NACH_BORROWER = "B_NACH_BORROWER"

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
        "principal": 4200,
        "interest": 650,
        "charges": 150,
        "due_date": "2026-07-10",
        "loan_tenure_months": 36,
        "interest_rate_pct": 12.5,
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
        "principal": 4200,
        "interest": 650,
        "charges": 150,
        "due_date": "2026-07-15",
        "loan_tenure_months": 24,
        "interest_rate_pct": 11.0,
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
    B_PROCESSING: {
        "loan_id": "LN-PROC-007",
        "amount_due": 5000,
        "dpd": 10,
        "bucket": "0-30",
        "principal": 4200,
        "interest": 650,
        "charges": 150,
        "payments": [
            {
                "payment_id": "PAY-3001",
                "amount": 5000,
                "date": "2026-06-24",
                "status": "processing",
            }
        ],
        "dispute_open": False,
        "followup_scheduled": False,
        "disposition": None,
        "payment_links": [],
        "simulate_errors": False,
    },
    B_CLOSED: {
        "loan_id": "LN-CLOSED-008",
        "amount_due": 0,
        "dpd": 0,
        "bucket": "current",
        "loan_status": "closed",
        "payments": [
            {
                "payment_id": "PAY-4001",
                "amount": 5000,
                "date": "2026-05-01",
                "status": "posted",
            }
        ],
        "dispute_open": False,
        "followup_scheduled": False,
        "disposition": None,
        "payment_links": [],
        "simulate_errors": False,
    },
    B_OVERDUE: {
        "loan_id": "LN-OVER-009",
        "amount_due": 5000,
        "dpd": 24,
        "bucket": "0-30",
        "due_date": "2026-06-01",
        "principal": 4200,
        "interest": 650,
        "charges": 150,
        "payments": [],
        "dispute_open": False,
        "followup_scheduled": False,
        "disposition": None,
        "payment_links": [],
        "simulate_errors": False,
    },
    B_NACH_LENDER: {
        "loan_id": "LN-NACH-L-010",
        "amount_due": 5000,
        "dpd": 5,
        "bucket": "0-30",
        "due_date": "2026-06-20",
        "payments": [
            {
                "payment_id": "PAY-5001",
                "amount": 5000,
                "date": "2026-06-20",
                "status": "failed",
                "nach_status": "lender_failed",
                "nach_failure_side": "lender",
            }
        ],
        "nach_status": "lender_failed",
        "nach_failure_side": "lender",
        "dispute_open": False,
        "followup_scheduled": False,
        "disposition": None,
        "payment_links": [],
        "simulate_errors": False,
    },
    B_NACH_BORROWER: {
        "loan_id": "LN-NACH-B-011",
        "amount_due": 5000,
        "dpd": 5,
        "bucket": "0-30",
        "due_date": "2026-06-20",
        "payments": [
            {
                "payment_id": "PAY-6001",
                "amount": 5000,
                "date": "2026-06-20",
                "status": "failed",
                "nach_status": "borrower_failed",
                "nach_failure_side": "borrower",
            }
        ],
        "nach_status": "borrower_failed",
        "nach_failure_side": "borrower",
        "dispute_open": False,
        "followup_scheduled": False,
        "disposition": None,
        "payment_links": [],
        "simulate_errors": False,
    },
}
