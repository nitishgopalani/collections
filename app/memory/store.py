from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

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
SESSIONS_PREFIX = "sessions:"
# PTP windows are ≤30d; keep the compact index a bit longer than that.
SESSIONS_TTL_SECONDS = 60 * 24 * 60 * 60
SESSIONS_MAX_RECORDS = 30


def sessions_key(borrower_id: str) -> str:
    return f"{SESSIONS_PREFIX}{borrower_id}"


def _merge_session_record(
    existing: list[dict[str, Any]],
    record: dict[str, Any],
) -> list[dict[str, Any]]:
    call_id = str(record.get("call_id") or "")
    merged = [r for r in existing if str(r.get("call_id") or "") != call_id]
    merged.append(dict(record))
    if len(merged) > SESSIONS_MAX_RECORDS:
        merged = merged[-SESSIONS_MAX_RECORDS:]
    return merged


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
        self._sessions: dict[str, list[dict[str, Any]]] = {}

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

    async def lookup_borrower_by_phone(
        self,
        phone: str,
        *,
        tenant_id: str = "default",
    ) -> BorrowerRecord | None:
        from app.util.phone import phone_match_suffix

        suffix = phone_match_suffix(phone)
        if not suffix or len(suffix) < 10:
            return None
        for record in self._borrowers.values():
            rec_phone = (record.comms_prefs or {}).get("phone") or ""
            if phone_match_suffix(rec_phone) == suffix:
                tenant = (record.compliance_flags or {}).get("tenant_id") or "default"
                if tenant_id and tenant not in {tenant_id, "default"}:
                    continue
                return record
        return None

    async def lookup_by_loan_ref(
        self,
        loan_ref: str,
        *,
        tenant_id: str = "default",
    ) -> BorrowerRecord | None:
        if not loan_ref:
            return None
        for record in self._borrowers.values():
            if str((record.loan or {}).get("account_ref") or "") == str(loan_ref):
                return record
        return None

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

    async def list_sessions(self, borrower_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._sessions.get(borrower_id, [])]

    async def upsert_session_record(self, borrower_id: str, record: dict[str, Any]) -> None:
        if not borrower_id:
            return
        current = list(self._sessions.get(borrower_id, []))
        self._sessions[borrower_id] = _merge_session_record(current, record)


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

    async def list_sessions(self, borrower_id: str) -> list[dict[str, Any]]:
        raw = await self._client.get(sessions_key(borrower_id))
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("sessions index corrupt borrower_id=%s", borrower_id)
            return []
        if not isinstance(payload, list):
            return []
        return [dict(item) for item in payload if isinstance(item, dict)]

    async def upsert_session_record(self, borrower_id: str, record: dict[str, Any]) -> None:
        if not borrower_id:
            return
        current = await self.list_sessions(borrower_id)
        merged = _merge_session_record(current, record)
        await self._client.set(
            sessions_key(borrower_id),
            json.dumps(merged, ensure_ascii=False),
            ttl_seconds=SESSIONS_TTL_SECONDS,
        )

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
