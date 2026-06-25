"""Re-export borrower fixtures for tests."""

from app.clients.tools_fixtures import (
    B_CLOSED,
    B_DUE,
    B_NACH_BORROWER,
    B_NACH_LENDER,
    B_OVERDUE,
    B_PAID,
    B_PARTIAL,
    B_PROCESSING,
    B_VERIFY_FAIL,
    B_VERIFY_OK,
    B_VULNERABLE,
    BORROWER_FIXTURES,
)

__all__ = [
    "B_PAID",
    "B_DUE",
    "B_PARTIAL",
    "B_VULNERABLE",
    "B_VERIFY_OK",
    "B_VERIFY_FAIL",
    "B_PROCESSING",
    "B_CLOSED",
    "B_OVERDUE",
    "B_NACH_LENDER",
    "B_NACH_BORROWER",
    "BORROWER_FIXTURES",
]
