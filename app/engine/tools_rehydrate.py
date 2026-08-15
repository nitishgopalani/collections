"""Mid-call borrower-state refetch (W3-2 hook, live/stub only)."""

from __future__ import annotations

import logging
from typing import Any

from app.clients.tools_contract import apply_borrower_state, unwrap_state_payload
from app.engine.call_history import tools_are_live
from app.schemas.state import BorrowerRecord, ConversationState

logger = logging.getLogger(__name__)


async def refetch_borrower_state(
    tools: Any,
    memory: Any,
    state: ConversationState,
    borrower: BorrowerRecord,
) -> tuple[BorrowerRecord, bool]:
    """Refetch borrower-state from stub/live tools. Returns (record, degraded).

    Simulate is a no-op (caller keeps the hydrated snapshot). Live timeout
    degrades to that snapshot. Stub reads the bound postgres/memory store.
    """
    if not tools_are_live(tools):
        return borrower, False

    slots = state.slots or {}
    args = {
        "borrower_id": state.borrower_id,
        "loan_ref": slots.get("account_ref") or (borrower.loan or {}).get("account_ref") or "",
        "phone": slots.get("phone") or slots.get("borrower_phone") or "",
        "account_ref": slots.get("account_ref") or "",
    }
    invoke = getattr(tools, "invoke", None)
    if not callable(invoke):
        return await _fallback_memory(memory, state, borrower), False

    try:
        raw = await invoke("get_borrower_state", args, state.tenant_id)
    except Exception:
        logger.warning("tools rehydrate invoke failed; keeping snapshot", exc_info=True)
        if hasattr(tools, "last_degraded"):
            tools.last_degraded = True
        return borrower, True

    if isinstance(raw, dict) and raw.get("degraded"):
        return borrower, True

    payload = unwrap_state_payload(raw if isinstance(raw, dict) else {})
    if payload and payload.get("found") is not False:
        return apply_borrower_state(borrower, payload), False

    return await _fallback_memory(memory, state, borrower), False


async def _fallback_memory(
    memory: Any,
    state: ConversationState,
    borrower: BorrowerRecord,
) -> BorrowerRecord:
    load = getattr(memory, "load_borrower", None)
    if not callable(load):
        return borrower
    fresh = await load(state.borrower_id)
    return fresh if fresh is not None else borrower
