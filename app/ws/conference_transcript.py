"""CF2.3 in-memory merged per-speaker conference transcripts.

Tap-only brain sessions (CF2.2) append labeled utterances keyed by
``parent_session_uuid``. Timestamps use a single server wall-clock source at
append time (milliseconds since epoch) — not per-session clocks and not ASR
capture time (v1).

Durable storage (DB) is a later production step; see PR_NOTES.md.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from app.engine.consult_binding import normalize_uuid

logger = logging.getLogger(__name__)

TIMESTAMP_SOURCE = "server_receive_ms"


@dataclass(frozen=True)
class TranscriptEntry:
    speaker: str
    text: str
    ts: int
    turn_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "speaker": self.speaker,
            "text": self.text,
            "ts": self.ts,
            "turn_id": self.turn_id,
        }


@dataclass
class _ConferenceRecord:
    parent_session_uuid: str
    entries: list[TranscriptEntry] = field(default_factory=list)
    finalized: bool = False
    finalized_at_ms: int | None = None
    last_touch_ms: int = 0


class ConferenceTranscriptStore:
    """Thread-safe in-memory store for merged tap transcripts."""

    def __init__(self, *, ttl_s: float = 3600.0) -> None:
        self._ttl_s = ttl_s
        self._lock = threading.Lock()
        self._by_parent: dict[str, _ConferenceRecord] = {}

    def configure_ttl(self, ttl_s: float) -> None:
        if ttl_s > 0:
            self._ttl_s = ttl_s

    def append_tap_turn(
        self,
        *,
        parent_session_uuid: str,
        speaker_label: str,
        text: str,
        turn_id: str,
        ts_ms: int | None = None,
    ) -> None:
        parent = normalize_uuid(parent_session_uuid)
        speaker = (speaker_label or "").strip()
        utterance = (text or "").strip()
        if not parent or not speaker or not utterance:
            return
        ts = ts_ms if ts_ms is not None else _wall_ms()
        with self._lock:
            self._expire_locked(_wall_ms())
            rec = self._by_parent.get(parent)
            if rec is None:
                rec = _ConferenceRecord(parent_session_uuid=parent, last_touch_ms=_wall_ms())
                self._by_parent[parent] = rec
            rec.entries.append(
                TranscriptEntry(
                    speaker=speaker,
                    text=utterance,
                    ts=ts,
                    turn_id=turn_id,
                )
            )
            rec.last_touch_ms = _wall_ms()

    def finalize(self, parent_session_uuid: str, *, ts_ms: int | None = None) -> None:
        parent = normalize_uuid(parent_session_uuid)
        if not parent:
            return
        now = ts_ms if ts_ms is not None else _wall_ms()
        with self._lock:
            self._expire_locked(now)
            rec = self._by_parent.get(parent)
            if rec is None:
                rec = _ConferenceRecord(parent_session_uuid=parent, last_touch_ms=_wall_ms())
                self._by_parent[parent] = rec
            rec.finalized = True
            rec.finalized_at_ms = now
            rec.last_touch_ms = _wall_ms()
            logger.info(
                "conference transcript finalized parent_session_uuid=%s entries=%d",
                parent,
                len(rec.entries),
            )

    def get_transcript(self, parent_session_uuid: str) -> dict[str, Any] | None:
        parent = normalize_uuid(parent_session_uuid)
        if not parent:
            return None
        with self._lock:
            self._expire_locked(_wall_ms())
            rec = self._by_parent.get(parent)
            if rec is None:
                return None
            return _build_payload(rec)

    def reset_for_tests(self) -> None:
        with self._lock:
            self._by_parent.clear()

    def _expire_locked(self, now_ms: int) -> None:
        if self._ttl_s <= 0:
            return
        cutoff = now_ms - int(self._ttl_s * 1000)
        stale = [
            key
            for key, rec in self._by_parent.items()
            if rec.last_touch_ms < cutoff
        ]
        for key in stale:
            del self._by_parent[key]


def _wall_ms() -> int:
    return int(time.time() * 1000)


def _build_payload(rec: _ConferenceRecord) -> dict[str, Any]:
    ordered = sorted(rec.entries, key=lambda e: (e.ts, e.turn_id))
    entries = [e.to_dict() for e in ordered]
    formatted = "\n".join(f"{e['speaker']}: {e['text']}" for e in entries)
    return {
        "parent_session_uuid": rec.parent_session_uuid,
        "finalized": rec.finalized,
        "finalized_at_ms": rec.finalized_at_ms,
        "timestamp_source": TIMESTAMP_SOURCE,
        "entries": entries,
        "formatted": formatted,
    }


def merge_entries(entries: list[TranscriptEntry]) -> list[TranscriptEntry]:
    """Return entries sorted by ts then turn_id (for tests and reuse)."""
    return sorted(entries, key=lambda e: (e.ts, e.turn_id))


_STORE = ConferenceTranscriptStore()


def get_store() -> ConferenceTranscriptStore:
    return _STORE


def append_tap_turn(**kwargs: Any) -> None:
    _STORE.append_tap_turn(**kwargs)


def finalize_conference(parent_session_uuid: str, *, ts_ms: int | None = None) -> None:
    _STORE.finalize(parent_session_uuid, ts_ms=ts_ms)


def get_merged_transcript(parent_session_uuid: str) -> dict[str, Any] | None:
    return _STORE.get_transcript(parent_session_uuid)
