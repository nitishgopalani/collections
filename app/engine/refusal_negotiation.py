"""Refusal & negotiation→human helpers (FS-7)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from app.schemas.state import BorrowerRecord, ConversationState

THREAT_FALSE_URGENCY_PHRASES: tuple[str, ...] = (
    "police",
    "jail",
    "last warning",
    "final warning",
    "aaj hi warna",
    "kidnap",
    "threaten",
    "bezati",
    "sharam",
    "property seize",
)

UNAUTHORIZED_TERM_PATTERNS: tuple[str, ...] = (
    r"\b\d+\s*%\s*(off|discount|waiver)\b",
    r"\bwaive\b",
    r"\bmoratorium\b.*\bmonths?\b",
    r"\bsettlement\b.*\b\d+",
    r"\brestructure\b.*\b\d+",
    r"\bapprove\b.*\b(settlement|waiver|moratorium)\b",
)

REVIEW_DISPOSITIONS: dict[str, str] = {
    "settlement": "SETTLEMENT_REVIEW",
    "restructure": "RESTRUCTURE_REVIEW",
    "moratorium": "MORATORIUM_REVIEW",
    "beyond_authority": "BEYOND_AUTHORITY_REVIEW",
}


def reply_has_threat_or_false_urgency(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text.lower().strip())
    return any(phrase in normalized for phrase in THREAT_FALSE_URGENCY_PHRASES)


def reply_quotes_unauthorized_terms(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text.lower().strip())
    for pattern in UNAUTHORIZED_TERM_PATTERNS:
        if re.search(pattern, normalized):
            return True
    return False


def risk_flag_names(state: ConversationState) -> set[str]:
    raw = state.slots.get("risk_flags") or []
    names: set[str] = set()
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                names.add(str(item.get("flag", "")))
            elif item:
                names.add(str(item))
    return {name for name in names if name}


def has_strategic_default_signal(state: ConversationState) -> bool:
    return "strategic_default" in risk_flag_names(state)


def has_settlement_fishing_signal(state: ConversationState) -> bool:
    return "settlement_fishing" in risk_flag_names(state)


def has_genuine_hardship_context(state: ConversationState, borrower: BorrowerRecord | None) -> bool:
    if state.slots.get("hardship_active") or state.slots.get("hardship_context"):
        return True
    if borrower and borrower.hardships:
        return any(
            str(entry.get("status", "")).lower() in {"corroborated", "documented", "reported"}
            for entry in borrower.hardships
        )
    return False


def build_refusal_record(state: ConversationState, *, reason: str) -> dict[str, Any]:
    return {
        "type": "refusal",
        "reason": reason,
        "ts": datetime.now(UTC).isoformat(),
        "source": state.flow_stack[-1].flow if state.flow_stack else "refusal",
    }


def build_negotiation_packet(
    state: ConversationState,
    review_type: str,
    *,
    request_detail: Any = None,
) -> dict[str, Any]:
    flags = sorted(risk_flag_names(state))
    fishing = "settlement_fishing" in flags
    return {
        "type": review_type,
        "request": request_detail or state.slots.get("negotiation_request") or "",
        "risk_flags": flags,
        "settlement_fishing_flagged": fishing,
        "recommendation": (
            "Human review required — settlement fishing flag present; "
            "do not auto-grant or auto-reject."
            if fishing and review_type == "settlement"
            else "Human review required — bot must not quote terms or auto-decide."
        ),
        "ts": datetime.now(UTC).isoformat(),
    }


def sync_refusal_negotiation_on_persist(
    borrower: BorrowerRecord,
    state: ConversationState,
) -> BorrowerRecord:
    updated = borrower.model_copy(deep=True)

    refusal = state.slots.get("refusal_record_pending")
    if refusal:
        notes = list(updated.notes)
        notes.append(dict(refusal))
        updated.notes = notes

    packet = state.slots.get("negotiation_packet_pending")
    if packet:
        notes = list(updated.notes)
        entry = dict(packet)
        entry["review_type"] = entry.pop("type", "negotiation")
        entry["type"] = "negotiation_packet"
        notes.append(entry)
        updated.notes = notes

    grievance = state.slots.get("grievance_record_pending")
    if grievance:
        notes = list(updated.notes)
        notes.append(dict(grievance))
        updated.notes = notes

    return updated
