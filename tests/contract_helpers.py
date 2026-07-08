"""Shared assertions/helpers for P1/P2 API contract tests."""

from __future__ import annotations


def expected_consult_start(session_uuid: str, to: str) -> dict:
    """Brain-side consult_start kwargs after P1/P2 (explicit dial policy)."""
    from app.config import get_settings

    s = get_settings()
    return {
        "session_uuid": session_uuid,
        "to": to,
        "caller_id": "",
        "ring_budget_s": s.consult_ring_budget_s,
        "max_attempts": s.consult_max_attempts,
        "retry_gap_s": s.consult_retry_gap_s,
    }
