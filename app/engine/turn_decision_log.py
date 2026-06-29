"""Structured INFO logging for live turn routing visibility."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.schemas.command import Command
from app.schemas.state import BorrowerRecord, ConversationState

logger = logging.getLogger(__name__)


def format_borrower_summary(borrower: BorrowerRecord) -> str:
    borrower_id = (borrower.borrower_id or "").strip()
    name = str(borrower.identity.get("name") or "").strip()
    amount = (borrower.loan or {}).get("amount_due", "")
    if not borrower_id or borrower_id == "unknown":
        if not name and amount in ("", None):
            return "none"
    parts = [borrower_id or "unknown", name or "-", str(amount) if amount not ("", None) else "-"]
    return "|".join(parts)


def summarize_kb_candidates(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return "[]"
    compact = [
        f"{c.get('name', '?')}:{float(c.get('score', 0)):.2f}"
        for c in candidates
        if c.get("name")
    ]
    return "[" + ",".join(compact[:8]) + "]"


def extract_start_flow(commands: list[Command]) -> str:
    for cmd in commands:
        if cmd.command == "start_flow" and cmd.flow:
            return str(cmd.flow)
    return ""


def extract_slots_set(commands: list[Command]) -> dict[str, Any]:
    slots: dict[str, Any] = {}
    for cmd in commands:
        if cmd.command == "set_slot" and cmd.name:
            slots[str(cmd.name)] = cmd.value
    return slots


def active_flow_step(state: ConversationState) -> tuple[str, int]:
    if not state.flow_stack:
        return "", -1
    frame = state.flow_stack[-1]
    return frame.flow, frame.step_index


def summarize_gate(verdict: str, reason: str | None, fallback_text: str, draft: str) -> str:
    reason = (reason or "").strip()
    if verdict == "allow":
        return "allow"
    if reason in {"opt_out_active", "outside_call_window", "attempt_cap_daily"}:
        return f"silent:{reason or verdict}"
    if verdict in {"block", "modify"}:
        label = "fallback" if fallback_text != draft else "fallback"
        return f"{label}:{reason or verdict}"
    return f"{verdict}:{reason or '-'}"


def log_turn_decision(
    *,
    session_id: str,
    transcript: str,
    borrower: BorrowerRecord,
    kb_candidates: list[dict[str, Any]],
    commands: list[Command],
    rejected_slots: list[str],
    state: ConversationState,
    reply_id: str | None,
    gate_verdict: str,
    gate_reason: str | None,
    draft_reply: str,
    final_reply: str,
) -> None:
    """One INFO line summarizing routing for docker compose logs brain."""
    active_flow, step = active_flow_step(state)
    slots_set = extract_slots_set(commands)
    payload = {
        "session_id": session_id,
        "transcript": transcript[:200],
        "borrower": format_borrower_summary(borrower),
        "kb_candidates": summarize_kb_candidates(kb_candidates),
        "llm_start_flow": extract_start_flow(commands),
        "active_flow": active_flow,
        "step": step,
        "slots_set": slots_set,
        "rejected_slots": rejected_slots,
        "reply_id": reply_id or "",
        "gate": summarize_gate(gate_verdict, gate_reason, final_reply, draft_reply),
    }
    logger.info("turn_decision %s", json.dumps(payload, ensure_ascii=False, default=str))
