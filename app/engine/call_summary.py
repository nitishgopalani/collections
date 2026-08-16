"""ONE JSON line at session end — what ops greps and the weekly report reads."""

from __future__ import annotations

import json
import logging
import statistics
from typing import Any

from app.engine.obligation_export import collect_flags
from app.schemas.state import ConversationState

logger = logging.getLogger(__name__)

_SESSIONS: dict[str, dict[str, Any]] = {}
_EMITTED: set[str] = set()


def reset_summaries() -> None:
    _SESSIONS.clear()
    _EMITTED.clear()


def record_turn(
    state: ConversationState,
    *,
    latency_ms: float = 0.0,
    llm_calls: int = 0,
) -> None:
    """Accumulate one turn. Safe to call on every persist."""
    sid = state.call_id
    if not sid:
        return
    bucket = _SESSIONS.setdefault(
        sid,
        {
            "session_id": sid,
            "tenant": state.tenant_id,
            "latencies": [],
            "llm_calls": [],
            "dispositions": [],
            "tool_degraded": 0,
        },
    )
    bucket["tenant"] = state.tenant_id or bucket.get("tenant")
    slots = state.slots or {}
    bucket["scenario"] = (
        slots.get("plo_scenario") or slots.get("sot_scenario") or bucket.get("scenario") or ""
    )
    bucket["latencies"].append(float(latency_ms or 0.0))
    bucket["llm_calls"].append(int(llm_calls or 0))
    disp = slots.get("disposition")
    if disp and str(disp) not in bucket["dispositions"]:
        bucket["dispositions"].append(str(disp))
    if slots.get("_tool_degraded") or slots.get("tool_degraded"):
        bucket["tool_degraded"] = int(bucket["tool_degraded"]) + 1
    bucket["ptp_date"] = slots.get("ptp_date") or slots.get("committed_date")
    bucket["ptp_amount"] = slots.get("ptp_amount") or slots.get("repay_amount")
    bucket["flags"] = collect_flags(slots)


def build_summary(session_id: str) -> dict[str, Any] | None:
    bucket = _SESSIONS.get(session_id)
    if not bucket:
        return None
    lats = [float(x) for x in bucket.get("latencies") or []]
    llms = [int(x) for x in bucket.get("llm_calls") or []]
    turns = len(lats) or len(llms)
    llm_free = sum(1 for n in llms if n == 0)
    p50 = statistics.median(lats) if lats else 0.0
    return {
        "session_id": session_id,
        "tenant": bucket.get("tenant") or "",
        "scenario": bucket.get("scenario") or "",
        "turns": turns,
        "dispositions": list(bucket.get("dispositions") or []),
        "ptp_date": bucket.get("ptp_date"),
        "ptp_amount": bucket.get("ptp_amount"),
        "latency_p50_ms": round(p50, 1),
        "latency_max_ms": round(max(lats), 1) if lats else 0.0,
        "llm_free_pct": round(100.0 * llm_free / turns, 1) if turns else 0.0,
        "tool_degraded": int(bucket.get("tool_degraded") or 0),
        "flags": list(bucket.get("flags") or []),
    }


def emit_call_summary(session_id: str) -> dict[str, Any] | None:
    """Log call_summary once. Returns the payload or None if already emitted / empty."""
    if not session_id or session_id in _EMITTED:
        return None
    payload = build_summary(session_id)
    if payload is None:
        return None
    _EMITTED.add(session_id)
    logger.info("call_summary %s", json.dumps(payload, ensure_ascii=False, default=str))
    return payload


def maybe_emit_on_end(state: ConversationState) -> dict[str, Any] | None:
    if state.slots.get("end_call"):
        return emit_call_summary(state.call_id)
    return None
