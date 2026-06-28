"""Resolve borrower from local Postgres on session_start (phone → record)."""

from __future__ import annotations

import logging
from typing import Any

from app.schemas.state import BorrowerRecord
from app.ws.borrower_context import (
    apply_borrower_context_to_record,
    normalize_borrower_context,
)
from app.ws.session import BrainWSSession

logger = logging.getLogger(__name__)


async def resolve_session_borrower(
    memory: Any,
    session: BrainWSSession,
) -> BorrowerRecord | None:
    """Lookup borrower by phone in local Postgres; merge session metadata."""
    lookup = getattr(memory, "lookup_borrower_by_phone", None)
    ctx = normalize_borrower_context(session.borrower_context)
    phone = ctx.get("phone") or ctx.get("borrower_phone")
    tenant_id = session.tenant_id or "default"

    record: BorrowerRecord | None = None
    if session.borrower_id and session.borrower_id not in {"", "unknown"}:
        record = await memory.load_borrower(session.borrower_id)

    if record is None and phone and callable(lookup):
        record = await lookup(phone, tenant_id=tenant_id)
        if record is not None:
            session.borrower_id = record.borrower_id

    if record is None:
        if not ctx and session.borrower_id in {"", "unknown"}:
            return None
        record = BorrowerRecord(borrower_id=session.borrower_id or "unknown")

    if ctx:
        record = apply_borrower_context_to_record(record, ctx)
    await memory.save_borrower(record)
    logger.info(
        "session borrower resolved borrower_id=%s name=%s amount_due=%s",
        record.borrower_id,
        record.identity.get("name", ""),
        (record.loan or {}).get("amount_due", ""),
    )
    return record
