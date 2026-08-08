"""Local Postgres borrower lookup — conversation state stays in memory/Upstash."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.schemas.state import BorrowerRecord
from app.util.phone import canonical_phone, digits_only, phone_match_suffix

logger = logging.getLogger(__name__)


def normalize_phone(phone: str) -> str:
    """Backward-compatible alias — prefer canonical_phone()."""
    return canonical_phone(phone) or digits_only(phone)


def row_to_borrower(row: dict[str, Any]) -> BorrowerRecord:
    amount = row.get("amount_due")
    if isinstance(amount, Decimal):
        amount = int(amount) if amount == amount.to_integral_value() else float(amount)
    loan: dict[str, Any] = {
        "amount_due": amount,
        "account_ref": row.get("account_ref"),
        "customer_name": row.get("name") or "",
    }
    # PaisaLo loan-detail fields (nullable; absent on non-paisalo rows).
    for key, col in (
        ("repay_amount", "repay_amount"),
        ("loan_amount", "loan_amount"),
        ("due_date", "due_date"),
        ("disbursal_date", "disbursal_date"),
        ("days_past_due", "days_past_due"),
        ("dpd", "dpd"),
        ("branch", "branch"),
        ("branch_address", "branch_address"),
        ("last_date_paid", "last_date_paid"),
        ("product", "product"),
    ):
        v = row.get(col)
        if v is not None:
            if isinstance(v, Decimal):
                v = int(v) if v == v.to_integral_value() else float(v)
            loan[key] = v
    npa = row.get("npa_flag")
    if npa is not None:
        loan["npa_flag"] = bool(npa)
    return BorrowerRecord(
        borrower_id=str(row["id"]),
        identity={"name": row.get("name") or ""},
        loan=loan,
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
                SELECT id, name, phone, amount_due, account_ref, language, tenant_id, created_at,
                       repay_amount, loan_amount, due_date, disbursal_date,
                       days_past_due, dpd, branch, branch_address,
                       last_date_paid, product, npa_flag
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
        suffix = phone_match_suffix(phone)
        if not suffix or len(suffix) < 10:
            return None
        pool = await self._pool_ready()
        async with pool.connection() as conn:
            row = await self._fetchone(
                conn,
                """
                SELECT id, name, phone, amount_due, account_ref, language, tenant_id, created_at,
                       repay_amount, loan_amount, due_date, disbursal_date,
                       days_past_due, dpd, branch, branch_address,
                       last_date_paid, product, npa_flag
                FROM borrowers
                WHERE tenant_id = %s
                  AND right(regexp_replace(phone, '[^0-9]', '', 'g'), 10) = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (tenant_id, suffix),
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
        try:
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
        except UniqueViolation:
            logger.warning(
                "borrower upsert skipped duplicate phone tenant_id=%s borrower_id=%s phone=%s",
                tenant_id,
                record.borrower_id,
                phone,
            )
            return
        logger.debug("borrower upserted postgres borrower_id=%s", record.borrower_id)
