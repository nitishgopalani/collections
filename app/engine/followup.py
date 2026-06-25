"""Follow-up call helpers (FS-6) — history hydration, PTP/link/callback handling."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.config import TenantConfig
from app.engine.compliance_rules import within_call_window
from app.schemas.state import BorrowerRecord, ConversationState

SCOLD_THREAT_PHRASES: tuple[str, ...] = (
    "sharam",
    "police",
    "last warning",
    "idiot",
    "defaulter",
    "bezati",
    "scold",
    "threaten",
)

ATTEMPT_TONE_LADDER: tuple[str, ...] = ("standard", "firm", "serious")


def reply_has_scold_or_threat(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text.lower().strip())
    return any(phrase in normalized for phrase in SCOLD_THREAT_PHRASES)


def tone_register_for_attempt(attempt: int) -> str:
    index = min(max(attempt - 1, 0), len(ATTEMPT_TONE_LADDER) - 1)
    return ATTEMPT_TONE_LADDER[index]


def apply_attempt_tone_register(state: ConversationState) -> ConversationState:
    """Escalate seriousness by attempt count — never aggression (gate caps pressure)."""
    updated = state.model_copy(deep=True)
    slots = dict(updated.slots)
    flags = slots.get("compliance_flags") or {}
    attempt = int(flags.get("attempts_today") or updated.attempts or 1)
    if attempt < 1:
        attempt = 1
    slots["tone_register"] = tone_register_for_attempt(attempt)
    slots["attempt_tone_applied"] = True
    updated.slots = slots
    return updated


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    return None


def find_open_ptp(ptps: list[dict[str, Any]], call_date: date) -> dict[str, Any] | None:
    pending: list[dict[str, Any]] = []
    for entry in ptps:
        status = str(entry.get("status", "pending")).lower()
        if status in {"pending", "open", "scheduled"}:
            pending.append(entry)
    if not pending:
        for entry in ptps:
            status = str(entry.get("status", "")).lower()
            promised = _parse_date(entry.get("promised_date"))
            if status not in {"kept", "broken"} and promised and promised <= call_date:
                pending.append(entry)
    if not pending:
        return None
    return max(pending, key=lambda item: str(item.get("promised_date") or ""))


def hydrate_followup_from_borrower(
    state: ConversationState,
    borrower: BorrowerRecord | None,
) -> ConversationState:
    """Load follow-up context from durable borrower memory (not a cold start)."""
    if borrower is None:
        return state
    updated = state.model_copy(deep=True)
    slots = dict(updated.slots)
    call_date_raw = slots.get("call_date") or slots.get("today")
    call_date = _parse_date(call_date_raw) or date.today()

    if borrower.ptps:
        slots["ptps"] = list(borrower.ptps)
        open_ptp = find_open_ptp(borrower.ptps, call_date)
        if open_ptp:
            slots["open_ptp_date"] = open_ptp.get("promised_date")
            slots["open_ptp_status"] = open_ptp.get("status", "pending")
            slots["ptp_date"] = open_ptp.get("promised_date")

    if borrower.broken_ptps:
        slots["broken_ptps"] = list(borrower.broken_ptps)

    if borrower.payment_links:
        slots["payment_links"] = list(borrower.payment_links)
        last_link = borrower.payment_links[-1]
        slots["pending_payment_link"] = last_link.get("link") or last_link.get("payment_link")
        slots["payment_link"] = slots.get("pending_payment_link")
        if last_link.get("amount") is not None:
            slots["link_amount"] = last_link.get("amount")

    callback = borrower.comms_prefs.get("scheduled_callback")
    if callback:
        slots["scheduled_callback"] = dict(callback)
        slots["followup_resume"] = True
        if callback.get("context"):
            slots["prior_call_context"] = callback.get("context")

    for note in reversed(borrower.notes):
        if note.get("type") == "call_context":
            slots["prior_call_context"] = note.get("text") or slots.get("prior_call_context")
            break

    updated.slots = slots
    return updated


def evaluate_ptp_followup(state: ConversationState) -> dict[str, bool]:
    slots = state.slots
    call_date = _parse_date(slots.get("call_date") or slots.get("today")) or date.today()
    ptp_date = _parse_date(slots.get("open_ptp_date") or slots.get("ptp_date"))
    kept = False
    broken = False
    if ptp_date is not None and call_date > ptp_date:
        broken = True
    status = str(slots.get("open_ptp_status") or "").lower()
    if status == "kept":
        kept = True
    return {"ptp_followup_kept": kept, "ptp_followup_broken": broken}


def validate_callback_window(
    raw: Any,
    tenant_cfg: TenantConfig,
    *,
    call_date: date | None = None,
) -> bool:
    """Ensure requested callback time falls inside compliant call-window hours."""
    if raw is None:
        return False
    text = str(raw).strip()
    if not text:
        return False
    tz = ZoneInfo(tenant_cfg.call_window_timezone)
    base = call_date or date.today()
    try:
        if "T" in text:
            requested = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if requested.tzinfo is None:
                requested = requested.replace(tzinfo=tz)
        else:
            hour = 10
            minute = 0
            match = re.search(r"(\d{1,2})(?::(\d{2}))?", text)
            if match:
                hour = int(match.group(1))
                minute = int(match.group(2) or 0)
            requested = datetime(base.year, base.month, base.day, hour, minute, tzinfo=tz)
    except ValueError:
        return False
    return within_call_window(tenant_cfg, requested)


def build_ptp_kept_record(state: ConversationState) -> dict[str, Any]:
    slots = state.slots
    paid_on = slots.get("call_date") or slots.get("today") or date.today().isoformat()
    return {
        "promised_date": slots.get("open_ptp_date") or slots.get("ptp_date"),
        "status": "kept",
        "paid_on": str(paid_on)[:10],
        "ts": datetime.now(UTC).isoformat(),
        "source": "ptp_followup",
    }


def build_ptp_broken_record(state: ConversationState) -> dict[str, Any]:
    slots = state.slots
    broken_on = slots.get("call_date") or slots.get("today") or date.today().isoformat()
    promised = slots.get("open_ptp_date") or slots.get("ptp_date")
    return {
        "promised_date": promised,
        "broken_on": str(broken_on)[:10],
        "ts": datetime.now(UTC).isoformat(),
        "source": "ptp_followup",
    }


def sync_followup_on_persist(
    borrower: BorrowerRecord,
    state: ConversationState,
) -> BorrowerRecord:
    updated = borrower.model_copy(deep=True)
    slots = state.slots

    kept = slots.get("ptp_record_pending")
    if kept:
        ptps = list(updated.ptps)
        promised = kept.get("promised_date")
        replaced = False
        for index, entry in enumerate(ptps):
            if str(entry.get("promised_date")) == str(promised):
                ptps[index] = {**dict(entry), **dict(kept)}
                replaced = True
                break
        if not replaced:
            ptps.append(dict(kept))
        updated.ptps = ptps

    broken = slots.get("broken_ptp_record_pending")
    if broken:
        broken_ptps = list(updated.broken_ptps)
        broken_ptps.append(dict(broken))
        updated.broken_ptps = broken_ptps
        ptps = list(updated.ptps)
        promised = broken.get("promised_date")
        for index, entry in enumerate(ptps):
            if str(entry.get("promised_date")) == str(promised):
                ptps[index] = {**dict(entry), "status": "broken"}
                break
        updated.ptps = ptps

    if slots.get("payment_link_record_pending"):
        links = list(updated.payment_links)
        links.append(dict(slots["payment_link_record_pending"]))
        updated.payment_links = links

    callback = slots.get("callback_pending")
    if callback:
        comms = dict(updated.comms_prefs)
        comms["scheduled_callback"] = dict(callback)
        comms["callback_requested"] = True
        updated.comms_prefs = comms

    note = slots.get("call_context_note_pending")
    if note:
        notes = list(updated.notes)
        notes.append(dict(note))
        updated.notes = notes

    return updated
