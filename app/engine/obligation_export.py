"""W3-3 post-call obligation loop — daily export files + webhook stub.

Canonical per-call record (R1) lands in ``exports/dispositions_YYYYMMDD.jsonl``
with a CSV mirror. Callback re-queue and flagged worklist are separate daily
files. Webhook is a stub interface only (no HTTP).
"""

from __future__ import annotations

import csv
import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.schemas.api import TurnRequest
from app.schemas.state import ConversationState

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")

RECORD_FIELDS = (
    "session_id",
    "borrower_id",
    "tenant",
    "scenario",
    "disposition",
    "ptp_date",
    "ptp_amount",
    "flags",
    "call_ts",
    "duration",
)

SKIP_DISPOSITIONS = frozenset({"ECHO_HOLD", "", "None"})

CALLBACK_DISPOSITIONS = frozenset(
    {
        "repair_callback_scheduled",
        "callback_request",
        "CALLBACK",
    }
)

FLAGGED_DISPOSITIONS = frozenset(
    {
        "VULNERABLE_FLAGGED",
        "THIRD_PARTY_FLAGGED",
        "complaint_raised",
        "dnc_requested",
    }
)

FLAG_SLOT_KEYS = (
    "ptp_beyond_policy",
    "payment_claimed",
    "complaint_raised",
    "multi_loan",
    "repair_callback_scheduled",
    "dnc_requested",
)

_STUB_EMITTED: list[dict[str, Any]] = []


def reset_webhook_stub() -> None:
    _STUB_EMITTED.clear()


def stub_emitted() -> list[dict[str, Any]]:
    return list(_STUB_EMITTED)


class WebhookStub:
    """Client API later — records locally, never posts."""

    def emit(self, record: dict[str, Any]) -> None:
        _STUB_EMITTED.append(dict(record))
        logger.info(
            "obligation_webhook_stub session_id=%s disposition=%s",
            record.get("session_id"),
            record.get("disposition"),
        )


_WEBHOOK = WebhookStub()


def exports_root() -> Path:
    return Path(os.environ.get("EXPORTS_DIR") or "exports")


def day_stamp(day: date) -> str:
    return day.strftime("%Y%m%d")


def transcript_snippet(text: str, max_words: int = 30) -> str:
    words = (text or "").split()
    return " ".join(words[:max_words])


def collect_flags(slots: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    for key in FLAG_SLOT_KEYS:
        val = slots.get(key)
        if val is True or (isinstance(val, str) and val.lower() in {"true", "1", "yes"}):
            flags.append(key)
    compliance = slots.get("compliance_flags") or {}
    if isinstance(compliance, dict):
        for key in ("dnc_requested", "third_party_suspected"):
            if compliance.get(key) and key not in flags:
                flags.append(key)
    return flags


def _day_from_state(state: ConversationState) -> date:
    raw = state.slots.get("call_date") or state.slots.get("today")
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, str) and len(raw) >= 10:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            pass
    return datetime.now(_IST).date()


def _duration_seconds(state: ConversationState, now: datetime) -> int:
    raw = state.slots.get("_call_started_ts") or state.slots.get("_session_ts")
    if not raw:
        return 0
    try:
        started = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return 0
    if started.tzinfo is None:
        started = started.replace(tzinfo=_IST)
    return max(0, int((now - started).total_seconds()))


def build_disposition_record(
    state: ConversationState,
    request: TurnRequest | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    clock = now or datetime.now(_IST)
    slots = state.slots
    disposition = slots.get("disposition")
    if slots.get("repair_callback_scheduled") and disposition in {
        None,
        "ESCALATED_UNCLEAR",
    }:
        disposition = "repair_callback_scheduled"
    if disposition == "CALLBACK":
        disposition = "callback_request"
    if slots.get("complaint_raised") and not disposition:
        disposition = "complaint_raised"
    scenario = (
        slots.get("plo_scenario")
        or slots.get("sot_scenario")
        or ""
    )
    return {
        "session_id": state.call_id,
        "borrower_id": state.borrower_id,
        "tenant": state.tenant_id or (request.tenant_id if request else ""),
        "scenario": scenario,
        "disposition": disposition,
        "ptp_date": slots.get("ptp_date") or slots.get("committed_date"),
        "ptp_amount": slots.get("ptp_amount") or slots.get("repay_amount"),
        "flags": collect_flags(slots),
        "call_ts": slots.get("_session_ts") or slots.get("_call_started_ts") or clock.isoformat(),
        "duration": _duration_seconds(state, clock),
    }


def should_export(record: dict[str, Any]) -> bool:
    disp = record.get("disposition")
    if disp is None or str(disp) in SKIP_DISPOSITIONS:
        return False
    return True


def is_callback_row(record: dict[str, Any]) -> bool:
    if record.get("disposition") in CALLBACK_DISPOSITIONS:
        return True
    flags = record.get("flags") or []
    return "repair_callback_scheduled" in flags


def is_worklist_row(record: dict[str, Any]) -> bool:
    if record.get("disposition") in FLAGGED_DISPOSITIONS:
        return True
    flags = record.get("flags") or []
    return bool(FLAGGED_DISPOSITIONS.intersection(flags))


def _upsert_jsonl(path: Path, record: dict[str, Any], key: str = "session_id") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    sid = record.get(key)
    rows = [r for r in rows if r.get(key) != sid]
    rows.append(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )
    return rows


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            out = dict(row)
            flags = out.get("flags")
            if isinstance(flags, list):
                out["flags"] = "|".join(str(f) for f in flags)
            writer.writerow({k: out.get(k, "") for k in fieldnames})


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def export_closed_call(
    state: ConversationState,
    request: TurnRequest | None = None,
    *,
    last_transcript: str = "",
    webhook: WebhookStub | None = None,
) -> dict[str, Any] | None:
    """Upsert this call into the day's disposition / callback / worklist files."""
    record = build_disposition_record(state, request)
    if not should_export(record):
        return None
    day = _day_from_state(state)
    stamp = day_stamp(day)
    root = exports_root()
    disp_jsonl = root / f"dispositions_{stamp}.jsonl"
    disp_csv = root / f"dispositions_{stamp}.csv"
    rows = _upsert_jsonl(disp_jsonl, record)
    _write_csv(disp_csv, rows, RECORD_FIELDS)

    if is_callback_row(record):
        cb_path = root / f"callbacks_{stamp}.jsonl"
        _upsert_jsonl(cb_path, record)

    if is_worklist_row(record):
        snippet = transcript_snippet(
            last_transcript
            or (request.transcript if request else "")
            or str(state.slots.get("_last_borrower_transcript") or ""),
        )
        work = dict(record)
        work["snippet"] = snippet
        wl_path = root / f"worklist_{stamp}.jsonl"
        _upsert_jsonl(wl_path, work)

    (webhook or _WEBHOOK).emit(record)
    return record
