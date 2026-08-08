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

    if not phone:
        logger.warning(
            "session borrower phone missing session_id=%s borrower_id=%s tenant_id=%s",
            session.session_id,
            session.borrower_id,
            tenant_id,
        )

    record: BorrowerRecord | None = None
    if session.borrower_id and session.borrower_id not in {"", "unknown"}:
        record = await memory.load_borrower(session.borrower_id)

    if record is None and phone and callable(lookup):
        # R2-DB: ignore sentinel/stale rows (borrower_id in {"","unknown"}) so a
        # malicious id="unknown" row can't be hydrated over the real seeded borrower.
        found = await lookup(phone, tenant_id=tenant_id)
        if found is None and tenant_id not in {"", "default"}:
            found = await lookup(phone, tenant_id="default")
        if found is not None and found.borrower_id not in {"", "unknown"}:
            record = found
            session.borrower_id = record.borrower_id

    if record is None:
        if not ctx and session.borrower_id in {"", "unknown"}:
            return None
        record = BorrowerRecord(borrower_id=session.borrower_id or "unknown")

    if ctx:
        record = apply_borrower_context_to_record(record, ctx)
    await memory.save_borrower(record)
    logger.info(
        "session borrower resolved borrower_id=%s name=%s amount_due=%s language=%s",
        record.borrower_id,
        record.identity.get("name", ""),
        (record.loan or {}).get("amount_due", ""),
        (record.comms_prefs or {}).get("language", ""),
    )
    return record


def resolve_asr_language(
    record: BorrowerRecord | None,
    *,
    locale: str = "hi-IN",
    borrower_context: dict[str, Any] | None = None,
) -> str:
    """Pick BCP-47 locale for Sarvam ASR (borrower DB > context > session locale > hi-IN)."""
    ctx = normalize_borrower_context(borrower_context)
    known_borrower = record is not None and record.borrower_id not in {"", "unknown"}
    if known_borrower:
        sources = (
            (record.comms_prefs or {}).get("language"),
            ctx.get("language"),
            locale,
        )
    else:
        sources = (
            ctx.get("language"),
            (record.comms_prefs or {}).get("language") if record else None,
            locale,
        )
    for source in sources:
        if source is not None and str(source).strip():
            return str(source).strip()
    return "hi-IN"
