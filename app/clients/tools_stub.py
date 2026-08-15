"""TOOLS_MODE=stub — real borrower-store reads, zero sim hangups/actions."""

from __future__ import annotations

import logging
import time
from typing import Any

from app.clients.tools_contract import (
    DISPOSITION_TOOLS,
    STATE_TOOLS,
    STUB_FORBIDDEN,
    borrower_state_from_record,
)
from app.engine.obligation_export import WebhookStub
from app.schemas.state import BorrowerRecord

logger = logging.getLogger(__name__)


class StubToolClient:
    """UAT default. Answers get_borrower_state from the bound memory/postgres store."""

    def __init__(self, source: Any | None = None) -> None:
        self._source = source
        self.last_call_ms: float = 0.0
        self.last_degraded: bool = False
        self._webhook = WebhookStub()

    @property
    def is_stub(self) -> bool:
        return True

    @property
    def mode(self) -> str:
        return "stub"

    def bind_source(self, source: Any) -> None:
        self._source = source

    async def ping(self) -> bool:
        source = self._source
        ping = getattr(source, "ping", None)
        if callable(ping):
            try:
                return bool(await ping())
            except Exception:
                logger.warning("stub tools source ping failed")
                return False
        return True

    async def invoke(
        self,
        tool: str,
        args: dict[str, Any],
        tenant_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        _ = idempotency_key
        started = time.perf_counter()
        self.last_degraded = False
        if tool in STUB_FORBIDDEN:
            logger.warning("stub refused side-effect tool=%s (zero sim actions)", tool)
            self.last_call_ms = (time.perf_counter() - started) * 1000.0
            return {"ok": False, "error": "stub_no_side_effects", "result": {}}
        if tool in STATE_TOOLS:
            result = await self.get_borrower_state(
                loan_ref=str(args.get("loan_ref") or args.get("account_ref") or ""),
                phone=str(args.get("phone") or ""),
                borrower_id=str(args.get("borrower_id") or ""),
                tenant_id=tenant_id,
            )
            self.last_call_ms = (time.perf_counter() - started) * 1000.0
            return result
        if tool in DISPOSITION_TOOLS:
            self._webhook.emit(dict(args))
            self.last_call_ms = (time.perf_counter() - started) * 1000.0
            return {"ok": True, "result": {"accepted": True, "stub": True}}
        self.last_call_ms = (time.perf_counter() - started) * 1000.0
        return {"ok": True, "result": {}}

    async def get_borrower_state(
        self,
        *,
        loan_ref: str = "",
        phone: str = "",
        borrower_id: str = "",
        tenant_id: str = "",
    ) -> dict[str, Any]:
        started = time.perf_counter()
        record = await self._lookup(
            loan_ref=loan_ref,
            phone=phone,
            borrower_id=borrower_id,
            tenant_id=tenant_id,
        )
        self.last_call_ms = (time.perf_counter() - started) * 1000.0
        self.last_degraded = False
        if record is None:
            return {"ok": True, "degraded": False, "result": {"found": False}}
        payload = borrower_state_from_record(record)
        payload["found"] = True
        return {"ok": True, "degraded": False, "result": payload}

    async def _lookup(
        self,
        *,
        loan_ref: str,
        phone: str,
        borrower_id: str,
        tenant_id: str,
    ) -> BorrowerRecord | None:
        source = self._source
        if source is None:
            return None
        if borrower_id:
            load = getattr(source, "load_borrower", None)
            if callable(load):
                found = await load(borrower_id)
                if found is not None:
                    return found
        if loan_ref:
            by_ref = getattr(source, "lookup_by_loan_ref", None)
            if callable(by_ref):
                found = await by_ref(loan_ref, tenant_id=tenant_id or "default")
                if found is not None:
                    return found
        if phone:
            by_phone = getattr(source, "lookup_borrower_by_phone", None) or getattr(
                source, "lookup_by_phone", None
            )
            if callable(by_phone):
                found = await by_phone(phone, tenant_id=tenant_id or "default")
                if found is not None:
                    return found
        return None
