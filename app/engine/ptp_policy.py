"""W3-1 PTP policy engine + computed slots.

Pure functions. Tenant policy comes from ``TenantRuntimeProfile.ptp_policy``
(profile field — invariant #9, no tenant string-compares). Missing policy
→ engine off.

Date verdicts: accept | counter | accept_flagged
Partial verdicts: ask_remainder | ask_full

Computed slots (renderer-visible, never LLM-computed):
  remaining_after, days_to_due, days_since_due.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app.engine.scripted_coercions import (
    _DEVANAGARI_DIGITS,
    _WORD_NUMBERS,
    _extract_committed_date,
    today_ist,
)
from app.schemas.command import Command

PTP_COUNTER_COUNT_KEY = "_ptp_counter_count"
PTP_COUNTER_DATE_KEY = "_ptp_counter_date"
PTP_OFFERED_DATE_KEY = "_ptp_offered_date"
PTP_AWAITING_KEY = "_ptp_awaiting"

_MONEY_WRITE_SLOTS = frozenset(
    {
        "plo_payment_intent",
        "plo_timeline",
        "committed_date",
        "offered_amount",
        "ptp_date",
        "ptp_amount",
    }
)

_FRACTION_RE = re.compile(
    r"(aadha|adha|आधा|आधा|half|50\s*%|50\s*percent|"
    r"pachaas\s*(?:percent|%)|पचास\s*(?:percent|%|प्रतिशत))",
    re.IGNORECASE,
)
_QUARTER_RE = re.compile(
    r"(chauthai|चौथाई|quarter|25\s*%|25\s*percent)",
    re.IGNORECASE,
)
_THREE_QUARTER_RE = re.compile(
    r"(teen\s*chauthai|तीन\s*चौथाई|75\s*%|75\s*percent)",
    re.IGNORECASE,
)
_PERCENT_RE = re.compile(
    r"(?P<n>\d+)\s*(?:%|percent|प्रतिशत)",
    re.IGNORECASE,
)
_DIGIT_AMOUNT_RE = re.compile(r"(?<!\d)(\d{2,7})(?!\d)")
_SCALE = {
    "sau": 100, "सौ": 100,
    "hazaar": 1000, "hazar": 1000, "हजार": 1000,
    "lakh": 100000, "लाख": 100000,
    "crore": 10000000, "करोड़": 10000000, "करोड": 10000000,
}
_WORD_AMOUNT_RE = re.compile(
    r"(?P<n>\d+|[a-zA-Z\u0900-\u097F]+)\s*"
    r"(?P<scale>sau|सौ|hazaar|hazar|हजार|lakh|लाख|crore|करोड़|करोड)",
    re.IGNORECASE,
)
_NEGATION_RE = re.compile(
    r"(nahi|nahin|नहीं|नही|no\b|mat\b|मत)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PtpPolicyConfig:
    max_ptp_days: int = 30
    min_partial_pct: float = 25.0
    counter_max_attempts: int = 1

    @classmethod
    def from_mapping(cls, raw: Any) -> PtpPolicyConfig | None:
        if not isinstance(raw, dict) or not raw:
            return None
        return cls(
            max_ptp_days=int(raw.get("max_ptp_days", 30)),
            min_partial_pct=float(raw.get("min_partial_pct", 25)),
            counter_max_attempts=int(raw.get("counter_max_attempts", 1)),
        )


@dataclass(frozen=True)
class PtpVerdict:
    action: str
    ptp_date: str | None = None
    counter_date: str | None = None
    offered_amount: int | None = None
    remaining_after: int | None = None
    flagged: bool = False
    reason: str = ""


def policy_from_profile(profile: Any) -> PtpPolicyConfig | None:
    if profile is None:
        return None
    return PtpPolicyConfig.from_mapping(getattr(profile, "ptp_policy", None))


def _as_date(value: str | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip().replace(",", "")
    try:
        return int(float(text))
    except ValueError:
        return None


def nearest_acceptable_date(today: date, max_ptp_days: int) -> date:
    return today + timedelta(days=max(0, int(max_ptp_days)))


def evaluate_date(
    committed_date: str | date | None,
    *,
    policy: PtpPolicyConfig,
    today: date,
    counter_attempts: int = 0,
) -> PtpVerdict:
    """(committed_date, policy, today) → accept | counter | accept_flagged."""
    parsed = _as_date(committed_date)
    if parsed is None:
        return PtpVerdict(action="accept", reason="no_date")
    delta = (parsed - today).days
    iso = parsed.isoformat()
    if delta < 0:
        return PtpVerdict(action="accept", ptp_date=iso, reason="past_or_today")
    if delta <= policy.max_ptp_days:
        return PtpVerdict(action="accept", ptp_date=iso, reason="within_policy")
    counter = nearest_acceptable_date(today, policy.max_ptp_days)
    if counter_attempts >= policy.counter_max_attempts:
        return PtpVerdict(
            action="accept_flagged",
            ptp_date=iso,
            counter_date=counter.isoformat(),
            flagged=True,
            reason="beyond_policy_after_counter",
        )
    return PtpVerdict(
        action="counter",
        ptp_date=iso,
        counter_date=counter.isoformat(),
        reason="beyond_policy",
    )


def evaluate_partial(
    offered_amount: int,
    repay_amount: int,
    *,
    policy: PtpPolicyConfig,
) -> PtpVerdict:
    """Partial offer → ask_remainder (≥ min_pct) or ask_full (< min_pct)."""
    repay = max(int(repay_amount), 0)
    offered = max(int(offered_amount), 0)
    if repay <= 0:
        return PtpVerdict(action="ask_full", offered_amount=offered, reason="no_repay")
    if offered >= repay:
        return PtpVerdict(
            action="accept",
            offered_amount=offered,
            remaining_after=0,
            reason="full_amount",
        )
    pct = 100.0 * offered / repay
    remaining = max(repay - offered, 0)
    if pct + 1e-9 < policy.min_partial_pct:
        return PtpVerdict(
            action="ask_full",
            offered_amount=offered,
            remaining_after=remaining,
            reason="below_min_partial",
        )
    return PtpVerdict(
        action="ask_remainder",
        offered_amount=offered,
        remaining_after=remaining,
        reason="partial_ok",
    )


def extract_offered_amount(transcript: str, repay_amount: int | None) -> int | None:
    """Digits + Hindi number words + aadha/quarter fractions. None if no cue."""
    raw = (transcript or "").strip()
    if not raw:
        return None
    text = raw.translate(_DEVANAGARI_DIGITS)
    # Day-counts ("10 din baad") are dates, not rupees.
    text = re.sub(
        r"\d+\s*(?:din|दिन|day|days)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    repay = _as_int(repay_amount)

    if _THREE_QUARTER_RE.search(text) and repay:
        return max(int(round(repay * 0.75)), 1)
    if _FRACTION_RE.search(text) and repay:
        return max(int(round(repay * 0.5)), 1)
    if _QUARTER_RE.search(text) and repay:
        return max(int(round(repay * 0.25)), 1)

    pct = _PERCENT_RE.search(text)
    if pct and repay:
        n = int(pct.group("n"))
        if 0 < n < 100:
            return max(int(round(repay * n / 100.0)), 1)

    scaled: list[int] = []
    for m in _WORD_AMOUNT_RE.finditer(text):
        n_raw = m.group("n")
        scale = _SCALE.get(m.group("scale").lower()) or _SCALE.get(m.group("scale"))
        if not scale:
            continue
        if n_raw.isdigit():
            n = int(n_raw)
        else:
            n = _WORD_NUMBERS.get(n_raw.lower()) or _WORD_NUMBERS.get(n_raw)
            if n is None:
                continue
        scaled.append(n * scale)
    if scaled:
        return max(scaled)

    digits = [int(x) for x in _DIGIT_AMOUNT_RE.findall(text)]
    if digits:
        return max(digits)

    # Bare Hindi ones (ek..tees) only when a pay-verb is present, so
    # "das din baad" is not an amount.
    if re.search(r"(dunga|dung[aá]|दे\s*दू|रुपये|rupaye|rs\b|₹)", text, re.IGNORECASE):
        for token in re.findall(r"[a-zA-Z\u0900-\u097F]+", text.lower()):
            n = _WORD_NUMBERS.get(token)
            if n and n >= 1:
                return n
    return None


def compute_derived_slots(
    slots: dict[str, Any],
    today: date | None = None,
) -> dict[str, Any]:
    """Deterministic renderer slots. LLM never computes these."""
    today = today or today_ist(slots.get("call_date") or slots.get("today"))
    out: dict[str, Any] = {}
    repay = _as_int(slots.get("repay_amount") or slots.get("amount_due"))
    offered = _as_int(slots.get("offered_amount") or slots.get("ptp_amount"))
    if repay is not None:
        if offered is None:
            out["remaining_after"] = repay
        else:
            out["remaining_after"] = max(repay - offered, 0)
    due = _as_date(slots.get("due_date"))
    if due is not None:
        delta = (due - today).days
        out["days_to_due"] = max(delta, 0)
        out["days_since_due"] = max(-delta, 0)
    return out


def _has_slot_write(commands: list[Command], name: str) -> bool:
    return any(
        c.command == "set_slot" and c.name == name and str(c.value or "").strip()
        for c in commands
    )


def _slot_value(commands: list[Command], name: str) -> Any:
    for c in reversed(commands):
        if c.command == "set_slot" and c.name == name:
            return c.value
    return None


def _strip_money_writes(commands: list[Command]) -> list[Command]:
    return [
        c
        for c in commands
        if not (c.command == "set_slot" and c.name in _MONEY_WRITE_SLOTS)
    ]


def _confirmed(name: str, value: Any) -> Command:
    return Command(command="set_slot", name=name, value=value, source="confirmed")


def _compose(fid: str) -> Command:
    return Command(command="compose", fragments=[fid], oof_class="payment_assertion")


def _yes_to_counter(transcript: str, profile: Any, counter_date: str | None, today: date) -> bool:
    from app.engine.evidence_scorer import confirms_pending_value

    if not counter_date:
        return False
    return confirms_pending_value(
        transcript,
        profile,
        "willing",
        pending_date=counter_date,
        today=today,
    )


def apply_ptp_after_gate(
    *,
    apply_commands: list[Command],
    slots: dict[str, Any],
    transcript: str,
    profile: Any,
    today: date,
    gate_verdict: str,
    pending_confirm: dict | None,
    question_shape: bool,
) -> dict[str, Any]:
    """Post-gate PTP seam. Returns commands + slot writes + optional compose id.

    Gate-before-side-effect: caller must invoke this AFTER the Commitment Gate.
    """
    policy = policy_from_profile(profile)
    empty = {
        "commands": apply_commands,
        "slot_updates": {},
        "compose_id": None,
        "render_overlay": {},
        "verdict": None,
    }
    if policy is None or question_shape:
        return empty

    repay = _as_int(slots.get("repay_amount") or slots.get("amount_due"))
    awaiting = str(slots.get(PTP_AWAITING_KEY) or "")
    counter_attempts = int(slots.get(PTP_COUNTER_COUNT_KEY) or 0)
    offered_date = str(slots.get(PTP_OFFERED_DATE_KEY) or "") or None
    counter_date = str(slots.get(PTP_COUNTER_DATE_KEY) or "") or None

    candidate_date = _slot_value(apply_commands, "committed_date")
    if not candidate_date and isinstance(pending_confirm, dict):
        candidate_date = pending_confirm.get("committed_date")
    if not candidate_date:
        extracted = _extract_committed_date(transcript, today=today)
        if extracted:
            candidate_date = extracted

    # After a counter: haan → accept counter date; refuse / new far date → flag.
    if awaiting == "counter":
        if _yes_to_counter(transcript, profile, counter_date, today):
            iso = counter_date or candidate_date
            return _accept_result(
                apply_commands, iso, repay, flagged=False, reason="counter_accepted",
            )
        extracted = _extract_committed_date(transcript, today=today)
        if extracted:
            v = evaluate_date(
                extracted, policy=policy, today=today, counter_attempts=counter_attempts,
            )
            if v.action == "accept":
                return _accept_result(
                    apply_commands, v.ptp_date, repay, flagged=False, reason=v.reason,
                )
            return _accept_result(
                apply_commands,
                extracted,
                repay,
                flagged=True,
                reason="beyond_policy_after_counter",
            )
        if _NEGATION_RE.search(transcript or ""):
            return _accept_result(
                apply_commands,
                offered_date or candidate_date,
                repay,
                flagged=True,
                reason="counter_refused",
            )
        return empty

    if gate_verdict != "execute":
        return empty

    if not candidate_date:
        return empty

    v = evaluate_date(
        candidate_date, policy=policy, today=today, counter_attempts=counter_attempts,
    )
    if v.action == "counter":
        return {
            "commands": _strip_money_writes(apply_commands) + [_compose("ptp_counter_date")],
            "slot_updates": {
                PTP_AWAITING_KEY: "counter",
                PTP_COUNTER_COUNT_KEY: counter_attempts + 1,
                PTP_COUNTER_DATE_KEY: v.counter_date,
                PTP_OFFERED_DATE_KEY: v.ptp_date,
                "committed_date": v.ptp_date,
                "counter_date": v.counter_date,
            },
            "compose_id": "ptp_counter_date",
            "render_overlay": {"counter_date": v.counter_date, "committed_date": v.ptp_date},
            "verdict": v,
            "pending_counter": True,
        }
    flagged = v.action == "accept_flagged"
    return _accept_result(
        apply_commands, v.ptp_date or str(candidate_date), repay,
        flagged=flagged, reason=v.reason,
    )


def _accept_result(
    apply_commands: list[Command],
    ptp_date: str | None,
    repay: int | None,
    *,
    flagged: bool,
    reason: str,
) -> dict[str, Any]:
    cmds = [
        c
        for c in apply_commands
        if not (
            c.command == "set_slot"
            and c.name in {"plo_payment_intent", "plo_timeline"}
            and str(c.value or "").strip().lower() in {
                "refused", "refuse", "unwilling", "later", "denied", "no",
            }
        )
    ]
    if ptp_date and not _has_slot_write(cmds, "committed_date"):
        cmds.append(_confirmed("committed_date", ptp_date))
    if not _has_slot_write(cmds, "plo_payment_intent"):
        cmds.append(_confirmed("plo_payment_intent", "willing"))
    if not _has_slot_write(cmds, "plo_timeline"):
        cmds.append(_confirmed("plo_timeline", "specific_date"))
    if ptp_date:
        cmds.append(_confirmed("ptp_date", ptp_date))
    if repay is not None:
        cmds.append(_confirmed("ptp_amount", repay))
    cmds.append(_confirmed("disposition", "PTP_SET"))
    if flagged:
        cmds.append(_confirmed("ptp_beyond_policy", True))
    return {
        "commands": cmds,
        "slot_updates": {
            PTP_AWAITING_KEY: "",
            PTP_COUNTER_DATE_KEY: "",
        },
        "compose_id": None,
        "render_overlay": {},
        "verdict": PtpVerdict(
            action="accept_flagged" if flagged else "accept",
            ptp_date=ptp_date,
            flagged=flagged,
            reason=reason,
        ),
    }


def partial_pre_gate(
    *,
    commands: list[Command],
    transcript: str,
    slots: dict[str, Any],
    policy: PtpPolicyConfig,
) -> dict[str, Any] | None:
    """If transcript is a partial-amount offer, replace money writes with compose."""
    repay = _as_int(slots.get("repay_amount") or slots.get("amount_due"))
    offered = extract_offered_amount(transcript, repay)
    if offered is None or repay is None:
        return None
    v = evaluate_partial(offered, repay, policy=policy)
    if v.action not in {"ask_remainder", "ask_full"}:
        return None
    fid = "ptp_ack_remainder" if v.action == "ask_remainder" else "ptp_full_ask"
    overlay = {
        "offered_amount": offered,
        "remaining_after": v.remaining_after if v.remaining_after is not None else max(repay - offered, 0),
        "repay_amount": repay,
    }
    return {
        "commands": _strip_money_writes(commands) + [_compose(fid)],
        "compose_id": fid,
        "offered_amount": offered,
        "remaining_after": overlay["remaining_after"],
        "render_overlay": overlay,
        "verdict": v,
    }
