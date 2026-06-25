"""Hardship flow helpers (FS-3) — persist and validation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.schemas.state import BorrowerRecord, ConversationState

VALID_HARDSHIP_REASONS: frozenset[str] = frozenset(
    {
        "job_loss",
        "medical",
        "business",
        "reduced_income",
        "competing_obligations",
    }
)

HARDSHIP_PATHS: frozenset[str] = frozenset({"partial", "forbearance", "review"})

HARDSHIP_REASON_LABELS: dict[str, str] = {
    "job_loss": "naukri chale jaane",
    "medical": "medical situation",
    "business": "business slowdown",
    "reduced_income": "kam income",
    "competing_obligations": "doosri zimmedariyan",
}

PRESSURE_PHRASES: tuple[str, ...] = (
    "jama karna",
    "payment kar",
    "pay now",
    "aaj hi payment",
    "last warning",
    "due date",
    "amount due",
)


def normalize_hardship_reason(value: Any) -> str | None:
    if value is None:
        return None
    token = str(value).lower().strip().replace(" ", "_").replace("-", "_")
    if token in VALID_HARDSHIP_REASONS:
        return token
    aliases = {
        "jobloss": "job_loss",
        "naukri_chali_gayi": "job_loss",
        "hospital": "medical",
        "illness": "medical",
        "business_loss": "business",
        "income_down": "reduced_income",
        "competing": "competing_obligations",
    }
    return aliases.get(token)


def build_hardship_record(state: ConversationState) -> dict[str, Any]:
    reason = normalize_hardship_reason(state.slots.get("hardship_reason"))
    onset = state.slots.get("call_date") or state.slots.get("today")
    if onset is None:
        onset = datetime.now(UTC).date().isoformat()
    elif isinstance(onset, str):
        onset = onset[:10]
    status = "corroborated" if state.slots.get("hardship_corroborated") else "reported"
    if state.slots.get("partial_amount") or state.slots.get("hardship_path") == "partial":
        status = "corroborated"
    return {
        "type": reason or "unknown",
        "onset": onset,
        "expected_duration": state.slots.get("hardship_expected_duration"),
        "status": status,
        "ts": datetime.now(UTC).isoformat(),
        "source": "hardship_flow",
    }


def sync_hardships_on_persist(
    borrower: BorrowerRecord,
    state: ConversationState,
) -> BorrowerRecord:
    pending = state.slots.get("hardship_record_pending")
    if not pending:
        return borrower
    updated = borrower.model_copy(deep=True)
    hardships = list(updated.hardships)
    hardships.append(dict(pending))
    updated.hardships = hardships
    return updated


def has_corroborated_hardship_with_partials(borrower: BorrowerRecord) -> bool:
    corroborated = any(
        str(entry.get("status", "")).lower() in {"corroborated", "documented"}
        for entry in borrower.hardships
    )
    if not corroborated:
        return False
    return any(payment.get("partial") for payment in borrower.payments)


def reply_has_pressure_language(text: str) -> bool:
    normalized = text.lower()
    return any(phrase in normalized for phrase in PRESSURE_PHRASES)


def hardship_context_active(state: ConversationState) -> bool:
    if state.slots.get("hardship_active"):
        return True
    if not state.flow_stack:
        return False
    active = state.flow_stack[-1].flow
    return active in {"hardship", "vague_ptp"}
