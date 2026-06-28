from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.config import get_settings
from app.exceptions import StaleStateError
from app.memory.audit import audit_key, build_audit_record
from app.memory.upstash import UpstashRestClient
from app.schemas.state import BorrowerRecord, ConversationState, Event

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.memory.composite import CompositeMemoryStore

STATE_PREFIX = "state:"
BORROWER_PREFIX = "borrower:"


def state_key(call_id: str) -> str:
    return f"{STATE_PREFIX}{call_id}"


def borrower_key(borrower_id: str) -> str:
    return f"{BORROWER_PREFIX}{borrower_id}"


class InMemoryMemoryStore:
    """In-process store for STUB_MODE / CI (same concurrency rules as Upstash)."""

    def __init__(self) -> None:
        self._states: dict[str, ConversationState] = {}
        self._borrowers: dict[str, BorrowerRecord] = {}
        self._audits: dict[str, list[str]] = {}

    async def ping(self) -> bool:
        return True

    async def load_state(self, call_id: str) -> ConversationState | None:
        return self._states.get(call_id)

    async def save_state(self, state: ConversationState) -> None:
        expected_previous = state.version - 1
        existing = self._states.get(state.call_id)
        if existing is None:
            if expected_previous != 0:
                raise StaleStateError(expected_previous, -1)
        elif existing.version != expected_previous:
            raise StaleStateError(expected_previous, existing.version)
        self._states[state.call_id] = state.model_copy(deep=True)
        logger.debug(
            "state saved call_id=%s version=%s",
            state.call_id,
            state.version,
        )

    async def load_borrower(self, borrower_id: str) -> BorrowerRecord | None:
        return self._borrowers.get(borrower_id)

    async def save_borrower(self, record: BorrowerRecord) -> None:
        self._borrowers[record.borrower_id] = record.model_copy(deep=True)
        logger.debug("borrower saved borrower_id=%s", record.borrower_id)

    async def append_audit(
        self, event: Event, *, call_id: str, borrower_id: str, tenant_id: str = ""
    ) -> str:
        record = build_audit_record(
            event,
            call_id=call_id,
            borrower_id=borrower_id,
            tenant_id=tenant_id,
        )
        key = audit_key(borrower_id)
        self._audits.setdefault(key, []).append(record.to_json())
        return record.audit_id

    async def list_audit(self, borrower_id: str) -> list[str]:
        return list(self._audits.get(audit_key(borrower_id), []))


class UpstashMemoryStore:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._client = UpstashRestClient()

    async def ping(self) -> bool:
        if not self._settings.upstash_redis_rest_url or not self._settings.upstash_redis_rest_token:
            return False
        return await self._client.ping()

    async def load_state(self, call_id: str) -> ConversationState | None:
        raw = await self._client.get(state_key(call_id))
        if raw is None:
            return None
        state = ConversationState.model_validate_json(raw)
        logger.debug(
            "state loaded call_id=%s version=%s",
            call_id,
            state.version,
        )
        return state

    async def save_state(self, state: ConversationState) -> None:
        expected_previous = state.version - 1
        existing = await self.load_state(state.call_id)
        if existing is None:
            if expected_previous != 0:
                raise StaleStateError(expected_previous, -1)
        elif existing.version != expected_previous:
            raise StaleStateError(expected_previous, existing.version)

        payload = state.model_dump_json()
        await self._client.set(
            state_key(state.call_id),
            payload,
            ttl_seconds=self._settings.state_ttl_seconds,
        )
        logger.debug(
            "state saved call_id=%s version=%s",
            state.call_id,
            state.version,
        )

    async def load_borrower(self, borrower_id: str) -> BorrowerRecord | None:
        raw = await self._client.get(borrower_key(borrower_id))
        if raw is None:
            return None
        return BorrowerRecord.model_validate_json(raw)

    async def save_borrower(self, record: BorrowerRecord) -> None:
        await self._client.set(borrower_key(record.borrower_id), record.model_dump_json())
        logger.debug("borrower saved borrower_id=%s", record.borrower_id)

    async def append_audit(
        self, event: Event, *, call_id: str, borrower_id: str, tenant_id: str = ""
    ) -> str:
        record = build_audit_record(
            event,
            call_id=call_id,
            borrower_id=borrower_id,
            tenant_id=tenant_id,
        )
        await self._client.rpush(audit_key(borrower_id), record.to_json())
        logger.debug(
            "audit appended audit_id=%s borrower_id=%s kind=%s",
            record.audit_id,
            borrower_id,
            event.kind,
        )
        return record.audit_id

    async def list_audit(self, borrower_id: str) -> list[str]:
        return await self._client.lrange(audit_key(borrower_id))

    async def delete_keys(self, *keys: str) -> None:
        for key in keys:
            await self._client.execute(["DEL", key])


def create_memory_store() -> InMemoryMemoryStore | UpstashMemoryStore | CompositeMemoryStore:
    from app.memory.composite import CompositeMemoryStore
    from app.memory.postgres_borrowers import PostgresBorrowerStore

    settings = get_settings()
    state_store: InMemoryMemoryStore | UpstashMemoryStore
    if settings.memory_stub_mode:
        state_store = InMemoryMemoryStore()
    else:
        state_store = UpstashMemoryStore()

    borrower_url = settings.effective_borrower_database_url
    if borrower_url:
        borrower_store = PostgresBorrowerStore(borrower_url)
        logger.info("borrower store: local postgres (conversation state via %s)", type(state_store).__name__)
        return CompositeMemoryStore(state_store, borrower_store)

    return state_store
