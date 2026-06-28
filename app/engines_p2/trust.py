"""Trust Score Engine (Sprint 8 / blueprint Engine 4).

Pure deterministic scoring over borrower PTP/payment/event history.
Trust is an input to the decision/NLG layer — it is NOT a license to bypass
the compliance gate or safety pre-empt.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.schemas.state import BorrowerRecord, ConversationState

# Trust is an input, not a license — never wire this score into gate bypass paths.
TRUST_IS_INPUT_NOT_LICENSE = True

NEUTRAL_TRUST = 50
ANCHOR_BLEND = 0.44
RECENCY_DECAY = 0.22
PENALTY_MULTIPLIER = 1.15
REWARD_MULTIPLIER = 0.72

# Blueprint §10.1 delta ranges (min, max). Negative events: min is more severe.
DELTA_RANGES: dict[str, tuple[float, float]] = {
    "promise_kept": (8.0, 15.0),
    "payment_full_on_time": (10.0, 20.0),
    "partial_as_promised": (5.0, 10.0),
    "proactive_contact": (3.0, 8.0),
    "renegotiated_plan_honored": (6.0, 12.0),
    "broken_promise": (-25.0, -12.0),
    "repeated_excuse": (-12.0, -5.0),
    "ghosting_after_promise": (-20.0, -10.0),
    "inconsistent_story": (-15.0, -5.0),
    "partial_after_silence": (2.0, 5.0),
}


@dataclass(frozen=True)
class TrustEvent:
    event_type: str
    ts: datetime
    magnitude: float = 1.0
    meta: dict[str, Any] | None = None


@dataclass(frozen=True)
class TrustScoreResult:
    score: int
    accumulator: float
    anchor: float
    events_applied: int
    last_event_type: str | None


def clamp(value: float, lo: float = 0, hi: float = 100) -> float:
    return max(float(lo), min(float(hi), value))


def _parse_ts(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.min.replace(tzinfo=UTC)


def _event_delta(event_type: str, magnitude: float) -> float:
    lo, hi = DELTA_RANGES[event_type]
    mag = clamp(magnitude, 0.0, 1.0)
    raw = lo + mag * (hi - lo)
    if raw >= 0:
        return raw * REWARD_MULTIPLIER
    return raw * PENALTY_MULTIPLIER


def recency_weight(index_from_end: int) -> float:
    """Newer events weigh more — exponential decay by position from newest."""
    return math.exp(-RECENCY_DECAY * index_from_end)


def _kept_promise_ratio(borrower: BorrowerRecord, events: list[TrustEvent]) -> float:
    kept = sum(1 for e in events if e.event_type == "promise_kept")
    broken = sum(1 for e in events if e.event_type in ("broken_promise", "ghosting_after_promise"))
    total = kept + broken
    if total == 0:
        return 0.5
    return kept / total


def anchor_score(borrower: BorrowerRecord, events: list[TrustEvent]) -> float:
    """Lifetime kept-promise ratio anchor — resists one-off payment gaming."""
    ratio = _kept_promise_ratio(borrower, events)
    return 18.0 + ratio * 72.0


def accumulate_trust(
    events: list[TrustEvent],
    *,
    initial: float = NEUTRAL_TRUST,
) -> float:
    """Apply recency-weighted deltas in chronological order."""
    if not events:
        return float(initial)
    ordered = sorted(events, key=lambda e: e.ts)
    score = float(initial)
    n = len(ordered)
    for i, event in enumerate(ordered):
        if event.event_type not in DELTA_RANGES:
            continue
        idx_from_end = (n - 1) - i
        weight = recency_weight(idx_from_end)
        delta = _event_delta(event.event_type, event.magnitude)
        scale = 1.0
        if event.meta and "magnitude_scale" in event.meta:
            scale = float(event.meta["magnitude_scale"])
        score = clamp(score + delta * weight * scale)
    return score


def blend_with_anchor(accumulator: float, anchor: float) -> int:
    blended = accumulator * (1.0 - ANCHOR_BLEND) + anchor * ANCHOR_BLEND
    return int(round(clamp(blended)))


def compute_trust_score(
    borrower: BorrowerRecord,
    *,
    initial: float = NEUTRAL_TRUST,
) -> TrustScoreResult:
    events = collect_trust_events(borrower)
    accumulator = accumulate_trust(events, initial=initial)
    anchor = anchor_score(borrower, events)
    score = blend_with_anchor(accumulator, anchor)
    last_type = events[-1].event_type if events else None
    return TrustScoreResult(
        score=score,
        accumulator=accumulator,
        anchor=anchor,
        events_applied=len(events),
        last_event_type=last_type,
    )


def collect_trust_events(borrower: BorrowerRecord) -> list[TrustEvent]:
    """Normalize PTP, payment, excuse, and explicit trust notes into trust events."""
    events: list[TrustEvent] = []
    broken_dates: set[str] = set()

    for broken in borrower.broken_ptps:
        promised = str(broken.get("promised_date", ""))
        if promised:
            broken_dates.add(promised)
        ts = _parse_ts(broken.get("broken_on") or broken.get("ts") or broken.get("promised_date"))
        mag = float(broken.get("magnitude", 1.0))
        events.append(TrustEvent("broken_promise", ts, mag, broken))

    for ptp in borrower.ptps:
        promised = str(ptp.get("promised_date", ""))
        ts = _parse_ts(ptp.get("ts") or ptp.get("paid_on") or ptp.get("promised_date"))
        mag = float(ptp.get("magnitude", 1.0))
        status = str(ptp.get("status", "")).lower()
        if status == "kept":
            events.append(TrustEvent("promise_kept", ts, mag, ptp))
        elif status == "broken" and promised not in broken_dates:
            events.append(TrustEvent("broken_promise", ts, mag, ptp))

    for payment in borrower.payments:
        ts = _parse_ts(payment.get("ts") or payment.get("date"))
        mag = float(payment.get("magnitude", 1.0))
        meta = dict(payment)
        explicit = payment.get("event_type") or payment.get("trust_event")
        if explicit and explicit in DELTA_RANGES:
            events.append(TrustEvent(str(explicit), ts, mag, meta))
        elif payment.get("partial_after_silence"):
            events.append(TrustEvent("partial_after_silence", ts, mag, meta))
        elif payment.get("partial"):
            events.append(TrustEvent("partial_as_promised", ts, mag, meta))
        elif payment.get("on_time", True) and payment.get("full", True):
            events.append(TrustEvent("payment_full_on_time", ts, mag, meta))

    excuse_counts: dict[str, int] = {}
    for excuse in sorted(borrower.excuses, key=lambda e: str(e.get("date", ""))):
        text = str(excuse.get("text", "")).lower().strip()
        ts = _parse_ts(excuse.get("ts") or excuse.get("date"))
        if not text:
            continue
        excuse_counts[text] = excuse_counts.get(text, 0) + 1
        if excuse_counts[text] >= 2:
            mag = min(1.0, excuse_counts[text] / 3.0)
            events.append(TrustEvent("repeated_excuse", ts, mag, excuse))

    for note in borrower.notes:
        if note.get("kind") != "trust_event":
            continue
        event_type = note.get("type") or note.get("event_type")
        if event_type in DELTA_RANGES:
            ts = _parse_ts(note.get("ts"))
            mag = float(note.get("magnitude", 1.0))
            events.append(TrustEvent(str(event_type), ts, mag, dict(note)))

    events.sort(key=lambda e: e.ts)
    return events


def refresh_borrower_trust(
    borrower: BorrowerRecord,
    *,
    trigger: str | None = None,
    ts: datetime | None = None,
) -> BorrowerRecord:
    """Recompute trust from stored history and append trust_history when score changes."""
    result = compute_trust_score(borrower)
    updated = borrower.model_copy(deep=True)
    stamp = (ts or datetime.now(UTC)).isoformat()
    prior = updated.trust_current

    if result.score != prior or not updated.trust_history:
        entry: dict[str, Any] = {
            "ts": stamp,
            "score": result.score,
            "prior": prior,
            "trigger": trigger or result.last_event_type or "recompute",
            "accumulator": round(result.accumulator, 2),
            "anchor": round(result.anchor, 2),
        }
        updated.trust_history = [*updated.trust_history, entry]

    updated.trust_current = result.score
    return updated


def apply_trust_to_state(state: ConversationState, borrower: BorrowerRecord) -> ConversationState:
    """Expose cached trust on live call slots — read-only input for decision/NLG."""
    updated = state.model_copy(deep=True)
    slots = dict(updated.slots)
    slots["trust"] = borrower.trust_current
    updated.slots = slots
    return updated


def sync_trust_on_persist(
    borrower: BorrowerRecord,
    *,
    trigger: str = "turn_persist",
) -> BorrowerRecord:
    """Recompute trust from durable history during persist (off hot path)."""
    return refresh_borrower_trust(borrower, trigger=trigger)
