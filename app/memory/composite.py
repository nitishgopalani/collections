"""Composite store: conversation state in memory/Upstash, borrowers in local Postgres."""

from __future__ import annotations

import logging
from typing import Any

from app.memory.postgres_borrowers import PostgresBorrowerStore
from app.memory.store import InMemoryMemoryStore, UpstashMemoryStore
from app.schemas.state import BorrowerRecord, ConversationState, Event

logger = logging.getLogger(__name__)


class CompositeMemoryStore:
    """State/audit via inner store; borrower CRUD via Postgres when configured."""

    def __init__(
        self,
        state_store: InMemoryMemoryStore | UpstashMemoryStore,
        borrower_store: PostgresBorrowerStore,
    ) -> None:
        self._state = state_store
        self._borrowers = borrower_store

    @property
    def borrower_db_enabled(self) -> bool:
        return True

    async def ping(self) -> bool:
        state_ok = await self._state.ping()
        borrower_ok = await self._borrowers.ping()
        if not borrower_ok:
            logger.warning("borrower postgres unavailable — no fallback to external DB")
        return state_ok and borrower_ok

    async def load_state(self, call_id: str) -> ConversationState | None:
        return await self._state.load_state(call_id)

    async def save_state(self, state: ConversationState) -> None:
        await self._state.save_state(state)

    async def load_borrower(self, borrower_id: str) -> BorrowerRecord | None:
        record = await self._borrowers.load_borrower(borrower_id)
        if record is not None:
            return record
        return await self._state.load_borrower(borrower_id)

    async def lookup_borrower_by_phone(
        self,
        phone: str,
        *,
        tenant_id: str = "default",
    ) -> BorrowerRecord | None:
        return await self._borrowers.lookup_by_phone(phone, tenant_id=tenant_id)

    async def save_borrower(self, record: BorrowerRecord) -> None:
        try:
            await self._borrowers.save_borrower(record)
        except Exception:
            logger.exception(
                "borrower postgres save failed borrower_id=%s — continuing with in-memory copy",
                record.borrower_id,
            )
        await self._state.save_borrower(record)

    async def append_audit(
        self,
        event: Event,
        *,
        call_id: str,
        borrower_id: str,
        tenant_id: str = "",
    ) -> str:
        return await self._state.append_audit(
            event,
            call_id=call_id,
            borrower_id=borrower_id,
            tenant_id=tenant_id,
        )

    async def list_audit(self, borrower_id: str) -> list[str]:
        return await self._state.list_audit(borrower_id)

    async def list_sessions(self, borrower_id: str) -> list[dict[str, Any]]:
        return await self._state.list_sessions(borrower_id)

    async def upsert_session_record(self, borrower_id: str, record: dict[str, Any]) -> None:
        await self._state.upsert_session_record(borrower_id, record)

    async def close(self) -> None:
        await self._borrowers.close()
