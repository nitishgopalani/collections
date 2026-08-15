"""C-2 multi-loan hydration: highest-DPD active row wins."""

from __future__ import annotations

from typing import Any

from app.schemas.state import BorrowerRecord


def _dpd(row: dict[str, Any]) -> int:
    raw = row.get("days_past_due")
    if raw is None:
        raw = row.get("dpd")
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _is_active(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or "active").strip().lower()
    return status in {"", "active", "open", "live"}


def loan_rows(borrower: BorrowerRecord | None) -> list[dict[str, Any]]:
    if borrower is None:
        return []
    rows = [dict(r) for r in (getattr(borrower, "loans", None) or []) if isinstance(r, dict)]
    if not rows and borrower.loan:
        rows = [dict(borrower.loan)]
    return rows


def select_winning_loan(rows: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, bool]:
    """Return (winning_row, multi_loan). Highest DPD among active rows."""
    if not rows:
        return None, False
    active = [r for r in rows if _is_active(r)]
    pool = active or rows
    winner = max(pool, key=_dpd)
    return winner, len(pool) > 1
