"""W4-1 campaign dialer controls — DNC, cadence, active-call lock, callback queue.

Brain is the policy source. Campaign originates must go through
``check_originate`` / ``commit_originate`` (HTTP: ``/dialer/v0``).
Mid-call consult/transfer/join is out of scope.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.engine.obligation_export import exports_root, read_jsonl

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")
_DIGITS = re.compile(r"\D+")

DNC_REASONS = frozenset({"dnc_requested", "dnc_suppressed"})


def normalize_phone(phone: str | None) -> str:
    digits = _DIGITS.sub("", phone or "")
    if len(digits) > 10:
        digits = digits[-10:]
    return digits


def day_stamp(day: date) -> str:
    return day.strftime("%Y%m%d")


def today_ist(now: datetime | None = None) -> date:
    clock = now or datetime.now(_IST)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=_IST)
    return clock.astimezone(_IST).date()


@dataclass
class OriginateDecision:
    allow: bool
    reason: str
    attempts_today: int = 0
    max_attempts: int = 2
    borrower_id: str = ""
    phone: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.allow,
            "reason": self.reason,
            "attempts_today": self.attempts_today,
            "max_attempts": self.max_attempts,
            "borrower_id": self.borrower_id,
            "phone": self.phone,
        }


@dataclass
class DialerControls:
    """Process-local lock + file-backed DNC / dials (EXPORTS_DIR)."""

    active: dict[str, str] = field(default_factory=dict)

    def reset(self) -> None:
        self.active.clear()

    def _keys(self, borrower_id: str = "", phone: str = "") -> list[str]:
        keys: list[str] = []
        bid = (borrower_id or "").strip()
        if bid:
            keys.append(f"b:{bid}")
        norm = normalize_phone(phone)
        if norm:
            keys.append(f"p:{norm}")
        return keys

    def dnc_path(self) -> Path:
        return exports_root() / "dnc.jsonl"

    def dials_path(self, day: date) -> Path:
        return exports_root() / f"dials_{day_stamp(day)}.jsonl"

    def _append_jsonl(self, path: Path, row: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def record_dnc(
        self,
        *,
        borrower_id: str = "",
        phone: str = "",
        tenant_id: str = "",
        source: str = "disposition",
        ts: str = "",
    ) -> None:
        row = {
            "borrower_id": borrower_id,
            "phone": normalize_phone(phone),
            "tenant_id": tenant_id,
            "source": source,
            "ts": ts or datetime.now(_IST).isoformat(),
        }
        self._append_jsonl(self.dnc_path(), row)
        logger.info(
            "dnc_recorded borrower_id=%s phone=%s source=%s",
            borrower_id,
            row["phone"],
            source,
        )

    def _dnc_keys(self) -> set[str]:
        keys: set[str] = set()
        root = exports_root()
        for path in [self.dnc_path(), *sorted(root.glob("dispositions_*.jsonl")), *sorted(root.glob("worklist_*.jsonl"))]:
            if not path.is_file():
                continue
            for row in read_jsonl(path):
                disp = str(row.get("disposition") or "")
                flags = row.get("flags") or []
                is_dnc = (
                    path.name == "dnc.jsonl"
                    or disp in DNC_REASONS
                    or "dnc_requested" in flags
                )
                if not is_dnc:
                    continue
                for key in self._keys(str(row.get("borrower_id") or ""), str(row.get("phone") or "")):
                    keys.add(key)
        return keys

    def is_dnc(self, borrower_id: str = "", phone: str = "") -> bool:
        banned = self._dnc_keys()
        return any(key in banned for key in self._keys(borrower_id, phone))

    def attempts_today(self, borrower_id: str, phone: str, day: date) -> int:
        path = self.dials_path(day)
        if not path.is_file():
            return 0
        want = set(self._keys(borrower_id, phone))
        if not want:
            return 0
        n = 0
        for row in read_jsonl(path):
            have = set(self._keys(str(row.get("borrower_id") or ""), str(row.get("phone") or "")))
            if want & have:
                n += 1
        return n

    def is_active(self, borrower_id: str = "", phone: str = "") -> bool:
        return any(key in self.active for key in self._keys(borrower_id, phone))

    def check_originate(
        self,
        *,
        borrower_id: str = "",
        phone: str = "",
        day: date | None = None,
        max_attempts: int = 2,
    ) -> OriginateDecision:
        when = day or today_ist()
        bid = (borrower_id or "").strip()
        norm = normalize_phone(phone)
        attempts = self.attempts_today(bid, norm, when)
        base = OriginateDecision(
            allow=False,
            reason="ok",
            attempts_today=attempts,
            max_attempts=max_attempts,
            borrower_id=bid,
            phone=norm,
        )
        if not bid and not norm:
            base.reason = "missing_identity"
            return base
        if self.is_dnc(bid, norm):
            base.reason = "dnc_suppressed"
            logger.info(
                "dnc_suppressed borrower_id=%s phone=%s",
                bid,
                norm,
            )
            return base
        if self.is_active(bid, norm):
            base.reason = "active_call"
            logger.info("active_call borrower_id=%s phone=%s", bid, norm)
            return base
        if attempts >= max_attempts:
            base.reason = "cadence_blocked"
            logger.info(
                "cadence_blocked borrower_id=%s phone=%s attempts_today=%s max=%s",
                bid,
                norm,
                attempts,
                max_attempts,
            )
            return base
        base.allow = True
        return base

    def commit_originate(
        self,
        *,
        borrower_id: str = "",
        phone: str = "",
        tenant_id: str = "",
        day: date | None = None,
        max_attempts: int = 2,
        lock_id: str = "",
    ) -> OriginateDecision:
        when = day or today_ist()
        decision = self.check_originate(
            borrower_id=borrower_id,
            phone=phone,
            day=when,
            max_attempts=max_attempts,
        )
        if not decision.allow:
            return decision
        row = {
            "borrower_id": decision.borrower_id,
            "phone": decision.phone,
            "tenant_id": tenant_id,
            "ts": datetime.now(_IST).isoformat(),
            "day": day_stamp(when),
        }
        self._append_jsonl(self.dials_path(when), row)
        token = lock_id or decision.borrower_id or decision.phone
        for key in self._keys(decision.borrower_id, decision.phone):
            self.active[key] = token
        decision.attempts_today += 1
        logger.info(
            "dial_committed borrower_id=%s phone=%s attempts_today=%s",
            decision.borrower_id,
            decision.phone,
            decision.attempts_today,
        )
        return decision

    def release(self, borrower_id: str = "", phone: str = "") -> None:
        for key in self._keys(borrower_id, phone):
            self.active.pop(key, None)

    def consume_callbacks(
        self,
        day: date,
        *,
        max_attempts: int = 2,
        commit: bool = False,
    ) -> dict[str, Any]:
        path = exports_root() / f"callbacks_{day_stamp(day)}.jsonl"
        due: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for row in read_jsonl(path):
            bid = str(row.get("borrower_id") or "")
            phone = str(row.get("phone") or "")
            decision = self.check_originate(
                borrower_id=bid,
                phone=phone,
                day=day,
                max_attempts=max_attempts,
            )
            item = dict(row)
            item["gate"] = decision.as_dict()
            if decision.allow:
                if commit:
                    self.commit_originate(
                        borrower_id=bid,
                        phone=phone,
                        tenant_id=str(row.get("tenant") or row.get("tenant_id") or ""),
                        day=day,
                        max_attempts=max_attempts,
                    )
                due.append(item)
            else:
                skipped.append(item)
        return {
            "date": day_stamp(day),
            "due": due,
            "skipped": skipped,
        }


_CONTROLS = DialerControls()


def get_controls() -> DialerControls:
    return _CONTROLS


def reset_controls() -> None:
    _CONTROLS.reset()
