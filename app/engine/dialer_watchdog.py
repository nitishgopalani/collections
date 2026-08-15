"""W4-1 B2 — tripwire for originates that skipped /dialer/v0.

Zero enforcement. Logs ``dialer_bypass_detected`` when a non-inbound
session has no matching ``dials_*.jsonl`` row.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from app.engine.dialer_controls import get_controls, today_ist

logger = logging.getLogger(__name__)


def is_inbound_session(borrower_context: dict[str, Any] | None, turn_meta: dict[str, Any] | None = None) -> bool:
    ctx = borrower_context or {}
    meta = turn_meta or {}
    direction = str(ctx.get("direction") or meta.get("direction") or "").strip().lower()
    if direction == "inbound":
        return True
    if str(ctx.get("inbound_did") or "").strip():
        return True
    return False


def has_matching_dial(
    *,
    borrower_id: str = "",
    phone: str = "",
    day: date | None = None,
) -> bool:
    when = day or today_ist()
    return get_controls().attempts_today(borrower_id, phone, when) > 0


def maybe_flag_bypass(
    *,
    session_id: str,
    channel_id: str = "",
    borrower_id: str = "",
    phone: str = "",
    borrower_context: dict[str, Any] | None = None,
    day: date | None = None,
) -> bool:
    """Return True when a bypass was logged."""
    if is_inbound_session(borrower_context):
        return False
    if has_matching_dial(borrower_id=borrower_id, phone=phone, day=day):
        return False
    cid = channel_id or str((borrower_context or {}).get("channel_id") or session_id)
    logger.warning(
        "dialer_bypass_detected channel_id=%s session_id=%s borrower_id=%s phone=%s",
        cid,
        session_id,
        borrower_id,
        phone,
    )
    return True
