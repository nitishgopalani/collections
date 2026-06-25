"""Blueprint §10.3 canonical trust paths — hand-calibrated event sequences."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.engines_p2.trust import (
    TrustEvent,
    accumulate_trust,
    blend_with_anchor,
)
from app.schemas.state import BorrowerRecord

_ORIGIN = datetime(2026, 1, 1, 10, tzinfo=UTC)


def _mk(offset_days: int, event_type: str, magnitude: float = 1.0, **meta: float) -> TrustEvent:
    extra = {"magnitude_scale": meta["magnitude_scale"]} if "magnitude_scale" in meta else None
    return TrustEvent(event_type, _ORIGIN + timedelta(days=offset_days), magnitude, extra)


def _anchor_from_counts(kept: int, broken: int) -> float:
    total = kept + broken
    ratio = kept / total if total else 0.5
    return 18.0 + ratio * 72.0


def score_path(events: list[TrustEvent], *, kept: int, broken: int) -> int:
    acc = accumulate_trust(events)
    return blend_with_anchor(acc, _anchor_from_counts(kept, broken))


# Canonical §10.3 sequences calibrated to 88 / 32 / 58 / 30.
RELIABLE_EVENTS: list[TrustEvent] = [
    _mk(4, "payment_full_on_time", 0.43),
    _mk(9, "promise_kept", 0.45),
    _mk(14, "payment_full_on_time", 0.45),
    _mk(19, "proactive_contact", 0.34),
    _mk(24, "promise_kept", 0.46),
    _mk(29, "payment_full_on_time", 0.46),
    _mk(34, "renegotiated_plan_honored", 0.41),
    _mk(39, "promise_kept", 0.47),
    _mk(44, "payment_full_on_time", 0.47),
]

SLIPPING_EVENTS: list[TrustEvent] = [
    _mk(2, "payment_full_on_time", 0.74),
    _mk(7, "promise_kept", 0.74),
    _mk(12, "payment_full_on_time", 0.76),
    _mk(17, "promise_kept", 0.76),
    _mk(28, "broken_promise", 0.98),
    _mk(31, "repeated_excuse", 0.88),
    _mk(34, "broken_promise", 1.0),
    _mk(37, "ghosting_after_promise", 0.92),
    _mk(40, "inconsistent_story", 0.92),
    _mk(43, "broken_promise", 0.88),
    _mk(46, "partial_after_silence", 0.55),
]

RECOVERING_EVENTS: list[TrustEvent] = [
    _mk(4, "broken_promise", 0.92),
    _mk(9, "broken_promise", 0.94),
    _mk(16, "partial_after_silence", 0.73),
    _mk(20, "proactive_contact", 0.83),
    _mk(23, "partial_as_promised", 0.71),
    _mk(27, "renegotiated_plan_honored", 0.98),
    _mk(31, "promise_kept", 0.94),
    _mk(35, "partial_as_promised", 0.80, magnitude_scale=1.15),
]

GAMING_EVENTS: list[TrustEvent] = [
    _mk(3, "broken_promise", 1.0),
    _mk(8, "broken_promise", 1.0),
    _mk(12, "ghosting_after_promise", 1.0),
    _mk(16, "broken_promise", 1.0),
    _mk(20, "inconsistent_story", 0.96),
    _mk(24, "payment_full_on_time", 0.82, magnitude_scale=1.44),
]

PATH_TARGETS = {
    "reliable": 88,
    "slipping": 32,
    "recovering": 58,
    "gaming": 30,
}


def events_to_borrower(borrower_id: str, events: list[TrustEvent]) -> BorrowerRecord:
    """Materialize a borrower record from canonical trust events for integration tests."""
    payments: list[dict] = []
    ptps: list[dict] = []
    broken_ptps: list[dict] = []
    notes: list[dict] = []

    for event in events:
        ts = event.ts.isoformat()
        meta = dict(event.meta or {})
        if event.event_type == "payment_full_on_time":
            row: dict = {
                "date": ts,
                "on_time": True,
                "full": True,
                "magnitude": event.magnitude,
            }
            if "magnitude_scale" in meta:
                row["magnitude_scale"] = meta["magnitude_scale"]
            payments.append(row)
        elif event.event_type == "promise_kept":
            ptps.append(
                {
                    "promised_date": ts,
                    "status": "kept",
                    "paid_on": ts,
                    "magnitude": event.magnitude,
                }
            )
        elif event.event_type == "broken_promise":
            broken_ptps.append({"promised_date": ts, "broken_on": ts, "magnitude": event.magnitude})
        elif event.event_type == "partial_as_promised":
            row = {"date": ts, "partial": True, "magnitude": event.magnitude}
            if "magnitude_scale" in meta:
                row["magnitude_scale"] = meta["magnitude_scale"]
            payments.append(row)
        elif event.event_type == "partial_after_silence":
            row = {
                "date": ts,
                "partial": True,
                "partial_after_silence": True,
                "magnitude": event.magnitude,
            }
            if "magnitude_scale" in meta:
                row["magnitude_scale"] = meta["magnitude_scale"]
            payments.append(row)
        else:
            row = {
                "kind": "trust_event",
                "type": event.event_type,
                "ts": ts,
                "magnitude": event.magnitude,
            }
            if "magnitude_scale" in meta:
                row["magnitude_scale"] = meta["magnitude_scale"]
            notes.append(row)

    return BorrowerRecord(
        borrower_id=borrower_id,
        payments=payments,
        ptps=ptps,
        broken_ptps=broken_ptps,
        notes=notes,
    )


def reliable_borrower() -> BorrowerRecord:
    return events_to_borrower("B_RELIABLE", RELIABLE_EVENTS)


def slipping_borrower() -> BorrowerRecord:
    return events_to_borrower("B_SLIPPING", SLIPPING_EVENTS)


def recovering_borrower() -> BorrowerRecord:
    return events_to_borrower("B_RECOVERING", RECOVERING_EVENTS)


def gaming_borrower() -> BorrowerRecord:
    return events_to_borrower("B_GAMING", GAMING_EVENTS)


def gaming_borrower_without_final_payment() -> BorrowerRecord:
    return events_to_borrower("B_GAMING_PRE", GAMING_EVENTS[:-1])
