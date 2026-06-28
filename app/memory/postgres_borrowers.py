"""Local Postgres borrower lookup — conversation state stays in memory/Upstash."""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.schemas.state import BorrowerRecord

logger = logging.getLogger(__name__)

_NON_DIGIT_RE = re.compile(r"\D")


def normalize_phone(phone: str) -> str:
    """Normalize phone for lookup: digits only, keep leading country code if present."""
    raw = str(phone).strip()
    if raw.startswith("+"):
        digits = _NON_DIGIT_RE.sub("", raw)
        return f"+{digits}" if digits else raw
    return _NON_DIGIT_RE.sub("", raw)


def row_to_borrower(row: dict[str, Any]) -> BorrowerRecord:
    amount = row.get("amount_due")
    if isinstance(amount, Decimal):
        amount = int(amount) if amount == amount.to_integral_value() else float(amount)
    return BorrowerRecord(
        borrower_id=str(row["id"]),
        identity={"name": row.get("name") or ""},
        loan={
            "amount_due": amount,
            "account_ref": row.get("account_ref"),
        },
        comms_prefs={
            "phone": row.get("phone") or "",
            "language": row.get("language") or "hi-IN",
        },
    )


class PostgresBorrowerStore:
    """Read borrowers from local Postgres (test stack only — not Supabase)."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._pool: AsyncConnectionPool | None = None

    async def _pool_ready(self) -> AsyncConnectionPool:
        if self._pool is None:
            self._pool = AsyncConnectionPool(
                conninfo=self._database_url,
                min_size=1,
                max_size=4,
                kwargs={"row_factory": dict_row},
                open=False,
            )
            await self._pool.open()
            logger.info("borrower postgres pool opened")
        return self._pool

    async def ping(self) -> bool:
        try:
            pool = await self._pool_ready()
            async with pool.connection() as conn:
                row = await conn.execute("SELECT 1 AS ok")
                fetched = await row.fetchone()
                return fetched is not None and fetched.get("ok") == 1
        except Exception:
            logger.exception("borrower postgres ping failed")
            return False

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _fetchone(
        self,
        conn: AsyncConnection[Any],
        query: str,
        params: tuple[Any, ...],
    ) -> dict[str, Any] | None:
        cur = await conn.execute(query, params)
        row = await cur.fetchone()
        return row

    async def load_borrower(self, borrower_id: str) -> BorrowerRecord | None:
        pool = await self._pool_ready()
        async with pool.connection() as conn:
            row = await self._fetchone(
                conn,
                """
                SELECT id, name, phone, amount_due, account_ref, language, tenant_id, created_at
                FROM borrowers
                WHERE id = %s
                LIMIT 1
                """,
                (borrower_id,),
            )
            if row is None:
                return None
            record = row_to_borrower(row)
            logger.debug("borrower loaded from postgres borrower_id=%s", borrower_id)
            return record

    async def lookup_by_phone(
        self,
        phone: str,
        *,
        tenant_id: str = "default",
    ) -> BorrowerRecord | None:
        normalized = normalize_phone(phone)
        if not normalized:
            return None
        pool = await self._pool_ready()
        async with pool.connection() as conn:
            row = await self._fetchone(
                conn,
                """
                SELECT id, name, phone, amount_due, account_ref, language, tenant_id, created_at
                FROM borrowers
                WHERE tenant_id = %s
                  AND (
                    phone = %s
                    OR regexp_replace(phone, '[^0-9+]', '', 'g') = %s
                    OR regexp_replace(phone, '[^0-9]', '', 'g') = %s
                  )
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (tenant_id, phone.strip(), normalized, _NON_DIGIT_RE.sub("", normalized)),
            )
            if row is None:
                return None
            record = row_to_borrower(row)
            logger.info(
                "borrower matched by phone tenant_id=%s borrower_id=%s",
                tenant_id,
                record.borrower_id,
            )
            return record

    async def save_borrower(self, record: BorrowerRecord) -> None:
        """Upsert identity/loan fields for per-call context merges (local test DB only)."""
        name = record.identity.get("name") or "unknown"
        phone = (record.comms_prefs or {}).get("phone") or ""
        amount = (record.loan or {}).get("amount_due") or 0
        account_ref = (record.loan or {}).get("account_ref")
        language = (record.comms_prefs or {}).get("language") or "hi-IN"
        tenant_id = record.compliance_flags.get("tenant_id") or "default"
        pool = await self._pool_ready()
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO borrowers (id, name, phone, amount_due, account_ref, language, tenant_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    phone = EXCLUDED.phone,
                    amount_due = EXCLUDED.amount_due,
                    account_ref = EXCLUDED.account_ref,
                    language = EXCLUDED.language
                """,
                (record.borrower_id, name, phone, amount, account_ref, language, tenant_id),
            )
            await conn.commit()
        logger.debug("borrower upserted postgres borrower_id=%s", record.borrower_id)
