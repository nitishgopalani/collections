"""Behavioral Risk Engine (Sprint 9 / blueprint Engine 5).

Deterministic pattern detection over borrower history — not single events.
Risk flags are inputs to the decision layer; they are NOT a license to bypass
the compliance gate or safety pre-empt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.schemas.state import BorrowerRecord, ConversationState

# Risk is an input, not a license — never wire flags into gate bypass paths.
RISK_IS_INPUT_NOT_LICENSE = True

# Detection uses behavior only (promises, payments, engagement) — never protected attributes.
FAIRNESS_BEHAVIOR_ONLY = True

SERIOUS_LABELS = frozenset({"strategic_default", "fraud_indicator"})
EXCUSE_RECYCLING_MIN = 3
PROMISE_BREAK_STREAK_MIN = 2
GHOSTING_BASELINE_MULTIPLIER = 2.5
GHOSTING_MIN_SILENCE_DAYS = 7
DECAY_FACTOR = 0.55
IMPROVEMENT_DECAY_FACTOR = 0.25
MIN_CONFIDENCE = 0.35
IMPROVEMENT_WINDOW_DAYS = 21

# Canonical excuse clusters — semantic grouping via keyword overlap (behavioral text only).
EXCUSE_CLUSTER_KEYWORDS: dict[str, tuple[str, ...]] = {
    "salary_delay": ("salary", "salary delay", "salary late", "salary nahi", "tankhwah"),
    "job_loss": ("job loss", "naukri", "lost job", "unemployed", "retrench"),
    "medical": ("hospital", "medical", "illness", "doctor", "health"),
    "travel": ("travel", "out of town", "abroad", "sheher se bahar"),
    "bank_issue": ("bank", "upi fail", "payment fail", "transaction fail", "net banking"),
    "family_emergency": ("family emergency", "ghar mein", "death", "funeral"),
}

SETTLEMENT_KEYWORDS = ("settlement", "one time", "ots", "discount", "kam kar do", "partial waiver")
DISMISSIVE_KEYWORDS = (
    "won't pay",
    "nahi dunga",
    "don't call",
    "sue me",
    "do whatever",
    "time pass",
    "gaming",
    "default karunga",
)
FRAUD_IDENTITY_KEYWORDS = (
    "identity theft",
    "not my loan",
    "fraud",
    "galat loan",
    "someone else took",
    "forged",
    "identity stolen",
)
PAYMENT_FAILURE_KEYWORDS = ("payment fail", "upi fail", "bank error", "transaction failed")
WRONG_NUMBER_KEYWORDS = ("wrong number", "galat number", "not me", "wrong person")


@dataclass(frozen=True)
class RiskFlag:
    flag: str
    confidence: float
    reason: str
    evidence: list[str] = field(default_factory=list)

    def as_dict(self, *, ts: str) -> dict[str, Any]:
        return {
            "flag": self.flag,
            "confidence": round(max(0.0, min(1.0, self.confidence)), 3),
            "reason": self.reason,
            "evidence": list(self.evidence),
            "ts": ts,
            "decayed": False,
        }


@dataclass(frozen=True)
class RiskDetectionResult:
    flags: list[RiskFlag]
    signals: dict[str, bool]


def _parse_ts(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _normalize_text(text: str) -> str:
    lowered = text.lower().strip()
    return re.sub(r"\s+", " ", lowered)


def cluster_excuse(text: str) -> str:
    """Map excuse text to a behavioral cluster id (language-agnostic keywords)."""
    normalized = _normalize_text(text)
    for cluster_id, keywords in EXCUSE_CLUSTER_KEYWORDS.items():
        if any(keyword in normalized for keyword in keywords):
            return cluster_id
    return normalized or "unknown"


def _text_hits(text: str, keywords: tuple[str, ...]) -> bool:
    normalized = _normalize_text(text)
    return any(keyword in normalized for keyword in keywords)


def _sorted_excuses(borrower: BorrowerRecord) -> list[tuple[datetime, str, str]]:
    rows: list[tuple[datetime, str, str]] = []
    for excuse in borrower.excuses:
        ts = _parse_ts(excuse.get("ts") or excuse.get("date"))
        text = str(excuse.get("text", "")).strip()
        if ts is None or not text:
            continue
        rows.append((ts, text, cluster_excuse(text)))
    rows.sort(key=lambda row: row[0])
    return rows


def _sorted_payments(borrower: BorrowerRecord) -> list[tuple[datetime, dict[str, Any]]]:
    rows: list[tuple[datetime, dict[str, Any]]] = []
    for payment in borrower.payments:
        ts = _parse_ts(payment.get("ts") or payment.get("date"))
        if ts is None:
            continue
        rows.append((ts, payment))
    rows.sort(key=lambda row: row[0])
    return rows


def _engagement_timestamps(borrower: BorrowerRecord) -> list[datetime]:
    """Borrower engagement touchpoints used for relative ghosting baseline."""
    stamps: list[datetime] = []
    for excuse in borrower.excuses:
        ts = _parse_ts(excuse.get("ts") or excuse.get("date"))
        if ts:
            stamps.append(ts)
    for ptp in borrower.ptps:
        ts = _parse_ts(ptp.get("ts") or ptp.get("promised_date"))
        if ts:
            stamps.append(ts)
    for payment, _ in _sorted_payments(borrower):
        stamps.append(payment)
    for note in borrower.notes:
        if note.get("engagement") or note.get("kind") == "engagement":
            ts = _parse_ts(note.get("ts"))
            if ts:
                stamps.append(ts)
    for emotion in borrower.emotions:
        ts = _parse_ts(emotion.get("ts"))
        if ts:
            stamps.append(ts)
    stamps.sort()
    return stamps


def borrower_baseline_gap_days(borrower: BorrowerRecord) -> float:
    """Median interval between engagement events — borrower-relative baseline."""
    stamps = _engagement_timestamps(borrower)
    if len(stamps) < 2:
        return 5.0
    gaps = [(stamps[i] - stamps[i - 1]).total_seconds() / 86400 for i in range(1, len(stamps))]
    gaps.sort()
    mid = len(gaps) // 2
    return gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2


def _payment_between(
    payments: list[tuple[datetime, dict[str, Any]]],
    start: datetime,
    end: datetime,
) -> bool:
    for ts, payment in payments:
        if start < ts <= end and (payment.get("full") or payment.get("amount", 0)):
            return True
    return False


def _partial_corroborates_hardship(
    payments: list[tuple[datetime, dict[str, Any]]],
    start: datetime,
    end: datetime,
) -> bool:
    for ts, payment in payments:
        if start < ts <= end and payment.get("partial"):
            return True
    return False


def has_benign_explanation(borrower: BorrowerRecord) -> bool:
    """Disconfirmation: payment failure, wrong number, or documented hardship."""
    if borrower.hardships:
        return True
    for note in borrower.notes:
        text = str(note.get("text", ""))
        if _text_hits(text, PAYMENT_FAILURE_KEYWORDS) or _text_hits(text, WRONG_NUMBER_KEYWORDS):
            return True
        if note.get("kind") == "hardship":
            return True
    for excuse in borrower.excuses:
        cluster = cluster_excuse(str(excuse.get("text", "")))
        if cluster in ("medical", "family_emergency", "job_loss"):
            return True
    return False


def has_recent_improvement(borrower: BorrowerRecord, *, reference: datetime) -> bool:
    cutoff = reference - timedelta(days=IMPROVEMENT_WINDOW_DAYS)
    for ptp in borrower.ptps:
        if str(ptp.get("status", "")).lower() == "kept":
            ts = _parse_ts(ptp.get("paid_on") or ptp.get("ts"))
            if ts and ts >= cutoff:
                return True
    for ts, payment in _sorted_payments(borrower):
        if ts >= cutoff and (payment.get("full") or payment.get("partial")):
            return True
    return False


def detect_excuse_recycling(
    borrower: BorrowerRecord,
    *,
    reference: datetime,
) -> RiskFlag | None:
    excuses = _sorted_excuses(borrower)
    payments = _sorted_payments(borrower)
    if len(excuses) < EXCUSE_RECYCLING_MIN:
        return None

    by_cluster: dict[str, list[tuple[datetime, str]]] = {}
    for ts, text, cluster in excuses:
        by_cluster.setdefault(cluster, []).append((ts, text))

    for cluster, entries in by_cluster.items():
        if len(entries) < EXCUSE_RECYCLING_MIN:
            continue
        unresolved_cycles = 0
        for i in range(1, len(entries)):
            prev_ts = entries[i - 1][0]
            cur_ts = entries[i][0]
            if _payment_between(payments, prev_ts, cur_ts):
                continue
            if _partial_corroborates_hardship(payments, prev_ts, cur_ts):
                return None
            unresolved_cycles += 1
        if unresolved_cycles >= EXCUSE_RECYCLING_MIN - 1:
            last_excuse_ts = entries[-1][0]
            if _payment_between(payments, last_excuse_ts, reference):
                return None
            confidence = min(0.95, 0.55 + 0.12 * len(entries))
            return RiskFlag(
                flag="excuse_recycling",
                confidence=confidence,
                reason=(
                    f"Same excuse cluster '{cluster}' reused {len(entries)} times "
                    "without resolving payment"
                ),
                evidence=[text for _, text in entries[-3:]],
            )
    return None


def _unique_broken_events(borrower: BorrowerRecord) -> list[datetime]:
    """Deduplicated broken-promise timestamps (broken_ptps + ptps, by promised date)."""
    seen: set[str] = set()
    events: list[datetime] = []

    for bp in borrower.broken_ptps:
        promised = str(bp.get("promised_date", ""))[:10]
        ts = _parse_ts(bp.get("broken_on") or bp.get("ts") or bp.get("promised_date"))
        if ts is None:
            continue
        key = promised or ts.date().isoformat()
        if key in seen:
            continue
        seen.add(key)
        events.append(ts)

    for ptp in borrower.ptps:
        if str(ptp.get("status", "")).lower() != "broken":
            continue
        promised = str(ptp.get("promised_date", ""))[:10]
        key = promised or ""
        if key and key in seen:
            continue
        ts = _parse_ts(ptp.get("ts") or ptp.get("promised_date"))
        if ts is None:
            continue
        seen.add(key or ts.date().isoformat())
        events.append(ts)

    events.sort()
    return events


def detect_promise_breaking(borrower: BorrowerRecord, *, reference: datetime) -> RiskFlag | None:
    kept = sum(1 for p in borrower.ptps if str(p.get("status", "")).lower() == "kept")
    broken_events = _unique_broken_events(borrower)
    broken = len(broken_events)
    total = kept + broken
    if total == 0 or not broken_events:
        return None

    streak = 1
    for i in range(len(broken_events) - 1, 0, -1):
        if (broken_events[i] - broken_events[i - 1]).days <= 45:
            streak += 1
        else:
            break

    kept_ratio = kept / total if total else 0.0
    recency_days = max(0, (reference - broken_events[-1]).days)
    recency_boost = max(0.0, 1.0 - recency_days / 60.0)

    if streak < PROMISE_BREAK_STREAK_MIN and not (total >= 3 and kept_ratio < 0.35):
        return None

    confidence = min(
        0.92,
        0.45 + 0.12 * streak + 0.25 * recency_boost + (0.15 if kept_ratio < 0.35 else 0.0),
    )
    return RiskFlag(
        flag="promise_breaking",
        confidence=confidence,
        reason=f"Broken-promise streak {streak}; kept ratio {kept_ratio:.0%}",
        evidence=[f"broken_streak={streak}", f"kept_ratio={kept_ratio:.2f}"],
    )


def detect_ghosting(borrower: BorrowerRecord, *, reference: datetime) -> RiskFlag | None:
    ptps = [
        _parse_ts(p.get("promised_date") or p.get("ts"))
        for p in borrower.ptps
        if _parse_ts(p.get("promised_date") or p.get("ts"))
    ]
    if not ptps:
        return None
    last_ptp = max(ptps)
    silence_days = (reference - last_ptp).total_seconds() / 86400
    baseline = borrower_baseline_gap_days(borrower)
    threshold = max(GHOSTING_MIN_SILENCE_DAYS, baseline * GHOSTING_BASELINE_MULTIPLIER)

    engagement_after_ptp = any(ts > last_ptp for ts in _engagement_timestamps(borrower))
    if engagement_after_ptp:
        return None
    if silence_days < threshold:
        return None

    confidence = min(0.9, 0.5 + (silence_days - threshold) / max(threshold, 1) * 0.25)
    return RiskFlag(
        flag="ghosting",
        confidence=confidence,
        reason=(
            f"Silence {silence_days:.0f}d after PTP exceeds borrower baseline "
            f"({baseline:.1f}d × {GHOSTING_BASELINE_MULTIPLIER})"
        ),
        evidence=[f"baseline_gap_days={baseline:.1f}", f"silence_days={silence_days:.1f}"],
    )


def _has_capacity_signal(borrower: BorrowerRecord) -> bool:
    full_payments = sum(1 for _, p in _sorted_payments(borrower) if p.get("full", True))
    if full_payments >= 1:
        return True
    if borrower.identity.get("income_verified"):
        return True
    if borrower.identity.get("prior_payment_capacity"):
        return True
    return False


def _has_dismissive_language(borrower: BorrowerRecord) -> bool:
    for note in borrower.notes:
        if _text_hits(str(note.get("text", "")), DISMISSIVE_KEYWORDS):
            return True
    for emotion in borrower.emotions:
        if _text_hits(str(emotion.get("text", "")), DISMISSIVE_KEYWORDS):
            return True
    return False


def detect_settlement_fishing(borrower: BorrowerRecord) -> RiskFlag | None:
    settlement_mentions = 0
    early_ask = False
    for note in borrower.notes:
        text = str(note.get("text", ""))
        if not _text_hits(text, SETTLEMENT_KEYWORDS):
            continue
        settlement_mentions += 1
        if note.get("early") or note.get("persistent"):
            early_ask = True
    for dispute in borrower.disputes:
        text = str(dispute.get("text", ""))
        if _text_hits(text, SETTLEMENT_KEYWORDS):
            settlement_mentions += 1

    if settlement_mentions == 0:
        return None
    capacity = _has_capacity_signal(borrower)
    hardship_documented = bool(borrower.hardships) or has_benign_explanation(borrower)
    if not (early_ask or (capacity and not hardship_documented)):
        return None

    confidence = min(0.85, 0.5 + 0.15 * settlement_mentions + (0.15 if capacity else 0.0))
    return RiskFlag(
        flag="settlement_fishing",
        confidence=confidence,
        reason="Settlement ask with capacity/hardship contradiction",
        evidence=[f"settlement_mentions={settlement_mentions}", f"capacity={capacity}"],
    )


def _identity_theft_signal(borrower: BorrowerRecord) -> bool:
    for note in borrower.notes:
        if _text_hits(str(note.get("text", "")), FRAUD_IDENTITY_KEYWORDS):
            return True
    return False


def _kyc_contradiction(borrower: BorrowerRecord) -> bool:
    if borrower.identity.get("kyc_mismatch"):
        return True
    if borrower.identity.get("kyc_status") == "contradicted":
        return True
    for note in borrower.notes:
        if note.get("kind") == "kyc_contradiction":
            return True
    return False


def _story_inconsistency(borrower: BorrowerRecord) -> bool:
    for note in borrower.notes:
        if note.get("kind") == "story_inconsistency" or note.get("inconsistent_story"):
            return True
    clusters = {cluster for _, _, cluster in _sorted_excuses(borrower)}
    if "job_loss" in clusters and _has_capacity_signal(borrower):
        full_recent = any(p.get("full") for _, p in _sorted_payments(borrower)[-2:])
        if full_recent:
            return True
    return False


def detect_fraud_indicator(borrower: BorrowerRecord, signals: dict[str, bool]) -> RiskFlag | None:
    converging = [
        signals.get("identity_theft_language", False),
        signals.get("kyc_contradiction", False),
        signals.get("story_inconsistency", False),
    ]
    if sum(converging) < 2:
        return None
    confidence = min(0.95, 0.55 + 0.15 * sum(converging))
    return RiskFlag(
        flag="fraud_indicator",
        confidence=confidence,
        reason="Multiple converging fraud signals (corroboration required)",
        evidence=[name for name, hit in zip(
            ("identity_theft", "kyc_contradiction", "story_inconsistency"),
            converging,
            strict=True,
        ) if hit],
    )


def detect_strategic_default(
    borrower: BorrowerRecord,
    signals: dict[str, bool],
) -> RiskFlag | None:
    if has_benign_explanation(borrower):
        return None
    converging = [
        signals.get("capacity_present", False),
        signals.get("promise_breaking", False),
        signals.get("dismissive_language", False),
        signals.get("excuse_recycling", False),
    ]
    if sum(converging) < 2:
        return None
    confidence = min(0.9, 0.5 + 0.12 * sum(converging))
    return RiskFlag(
        flag="strategic_default",
        confidence=confidence,
        reason="Capacity present while willingness signals absent (corroborated)",
        evidence=[name for name, hit in zip(
            ("capacity", "promise_breaking", "dismissive_language", "excuse_recycling"),
            converging,
            strict=True,
        ) if hit],
    )


def collect_signals(borrower: BorrowerRecord, *, reference: datetime) -> dict[str, bool]:
    return {
        "capacity_present": _has_capacity_signal(borrower),
        "dismissive_language": _has_dismissive_language(borrower),
        "identity_theft_language": _identity_theft_signal(borrower),
        "kyc_contradiction": _kyc_contradiction(borrower),
        "story_inconsistency": _story_inconsistency(borrower),
        "promise_breaking": detect_promise_breaking(borrower, reference=reference) is not None,
        "excuse_recycling": detect_excuse_recycling(borrower, reference=reference) is not None,
    }


def detect_risk_flags(
    borrower: BorrowerRecord,
    *,
    reference: datetime | None = None,
) -> RiskDetectionResult:
    """Run all pattern detectors; serious labels require corroboration."""
    now = reference or datetime.now(UTC)
    signals = collect_signals(borrower, reference=now)
    flags: list[RiskFlag] = []

    excuse = detect_excuse_recycling(borrower, reference=now)
    if excuse:
        flags.append(excuse)

    promise = detect_promise_breaking(borrower, reference=now)
    if promise:
        flags.append(promise)

    ghost = detect_ghosting(borrower, reference=now)
    if ghost:
        flags.append(ghost)

    settlement = detect_settlement_fishing(borrower)
    if settlement:
        flags.append(settlement)

    strategic = detect_strategic_default(borrower, signals)
    if strategic:
        flags.append(strategic)

    fraud = detect_fraud_indicator(borrower, signals)
    if fraud:
        flags.append(fraud)

    return RiskDetectionResult(flags=flags, signals=signals)


def merge_with_decay(
    previous: list[dict[str, Any]],
    detected: list[RiskFlag],
    borrower: BorrowerRecord,
    *,
    reference: datetime,
    ts: str,
) -> list[dict[str, Any]]:
    """Decay flags that no longer fire; strong decay when behavior improves."""
    detected_by_name = {flag.flag: flag for flag in detected}
    merged: list[dict[str, Any]] = []
    improved = has_recent_improvement(borrower, reference=reference)

    for old in previous:
        name = str(old.get("flag", ""))
        if name in detected_by_name:
            new_flag = detected_by_name.pop(name)
            entry = new_flag.as_dict(ts=ts)
            if improved:
                old_conf = float(old.get("confidence", entry["confidence"]))
                entry["confidence"] = round(old_conf * IMPROVEMENT_DECAY_FACTOR, 3)
                entry["decayed"] = True
            merged.append(entry)
            continue
        old_conf = float(old.get("confidence", 0.0))
        factor = IMPROVEMENT_DECAY_FACTOR if improved else DECAY_FACTOR
        new_conf = old_conf * factor
        if new_conf >= MIN_CONFIDENCE:
            merged.append(
                {
                    **old,
                    "confidence": round(new_conf, 3),
                    "decayed": True,
                    "ts": ts,
                }
            )

    for flag in detected_by_name.values():
        merged.append(flag.as_dict(ts=ts))

    merged.sort(key=lambda item: (-float(item.get("confidence", 0)), str(item.get("flag", ""))))
    return merged


def compute_risk_flags(
    borrower: BorrowerRecord,
    *,
    reference: datetime | None = None,
) -> list[dict[str, Any]]:
    now = reference or datetime.now(UTC)
    result = detect_risk_flags(borrower, reference=now)
    return merge_with_decay(
        borrower.risk_flags,
        result.flags,
        borrower,
        reference=now,
        ts=now.isoformat(),
    )


def refresh_borrower_risk(
    borrower: BorrowerRecord,
    *,
    trigger: str | None = None,
    reference: datetime | None = None,
) -> BorrowerRecord:
    now = reference or datetime.now(UTC)
    flags = compute_risk_flags(borrower, reference=now)
    updated = borrower.model_copy(deep=True)
    updated.risk_flags = flags
    return updated


def apply_risk_to_state(state: ConversationState, borrower: BorrowerRecord) -> ConversationState:
    updated = state.model_copy(deep=True)
    slots = dict(updated.slots)
    slots["risk_flags"] = list(borrower.risk_flags)
    updated.slots = slots
    return updated


def sync_risk_on_persist(
    borrower: BorrowerRecord,
    *,
    trigger: str = "turn_persist",
    reference: datetime | None = None,
) -> BorrowerRecord:
    """Recompute risk flags from durable history during persist (off hot path)."""
    return refresh_borrower_risk(borrower, trigger=trigger, reference=reference)
