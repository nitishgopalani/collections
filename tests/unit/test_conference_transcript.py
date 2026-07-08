"""CF2.3 merged conference transcript store tests."""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.ws.conference_transcript import (
    ConferenceTranscriptStore,
    TIMESTAMP_SOURCE,
    append_tap_turn,
    finalize_conference,
    get_merged_transcript,
    get_store,
)


@pytest.fixture(autouse=True)
def _clean_store():
    get_store().reset_for_tests()
    get_store().configure_ttl(3600.0)
    yield
    get_store().reset_for_tests()


def test_merged_timeline_three_speakers_ordered():
    parent = "aaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    append_tap_turn(
        parent_session_uuid=parent,
        speaker_label="caller",
        text="ek do teen",
        turn_id="t1",
        ts_ms=1000,
    )
    append_tap_turn(
        parent_session_uuid=parent,
        speaker_label="party-2",
        text="char paanch chhe",
        turn_id="t2",
        ts_ms=2000,
    )
    append_tap_turn(
        parent_session_uuid=parent,
        speaker_label="party-3",
        text="saat aath nau",
        turn_id="t3",
        ts_ms=1500,
    )
    finalize_conference(parent, ts_ms=5000)
    out = get_merged_transcript(parent)
    assert out is not None
    assert out["timestamp_source"] == TIMESTAMP_SOURCE
    assert out["finalized"] is True
    speakers = [e["speaker"] for e in out["entries"]]
    assert speakers == ["caller", "party-3", "party-2"]
    assert "caller: ek do teen" in out["formatted"]
    assert "party-2: char paanch chhe" in out["formatted"]
    assert "party-3: saat aath nau" in out["formatted"]


def test_different_parent_sessions_isolated():
    append_tap_turn(
        parent_session_uuid="parent-a",
        speaker_label="caller",
        text="only A",
        turn_id="a1",
        ts_ms=100,
    )
    append_tap_turn(
        parent_session_uuid="parent-b",
        speaker_label="caller",
        text="only B",
        turn_id="b1",
        ts_ms=100,
    )
    a = get_merged_transcript("parent-a")
    b = get_merged_transcript("parent-b")
    assert a is not None and b is not None
    assert a["entries"][0]["text"] == "only A"
    assert b["entries"][0]["text"] == "only B"


def test_uuid_normalization_dashes_and_hex():
    append_tap_turn(
        parent_session_uuid="e773ce10-a826-4515-b42a-31548aa31422",
        speaker_label="caller",
        text="hello",
        turn_id="t1",
        ts_ms=1,
    )
    out = get_merged_transcript("e773ce10a8264515b42a31548aa31422")
    assert out is not None
    assert out["parent_session_uuid"] == "e773ce10a8264515b42a31548aa31422"
    assert out["entries"][0]["text"] == "hello"


def test_ttl_expiry_removes_transcript():
    store = ConferenceTranscriptStore(ttl_s=0.05)
    store.append_tap_turn(
        parent_session_uuid="ttl-parent",
        speaker_label="caller",
        text="gone soon",
        turn_id="t1",
        ts_ms=1000,
    )
    time.sleep(0.08)
    assert store.get_transcript("ttl-parent") is None


def test_entry_ts_can_be_historical_without_ttl_purge():
    """TTL uses wall-clock storage time, not utterance ts."""
    store = ConferenceTranscriptStore(ttl_s=3600.0)
    store.append_tap_turn(
        parent_session_uuid="old-ts",
        speaker_label="caller",
        text="still here",
        turn_id="t1",
        ts_ms=1,
    )
    out = store.get_transcript("old-ts")
    assert out is not None
    assert out["entries"][0]["ts"] == 1
    assert out["entries"][0]["text"] == "still here"


def test_concurrent_appends_do_not_drop_records():
    store = ConferenceTranscriptStore()
    parent = "concurrent-parent"
    barrier = threading.Barrier(3)

    def worker(label: str, ts: int) -> None:
        barrier.wait(timeout=2)
        store.append_tap_turn(
            parent_session_uuid=parent,
            speaker_label=label,
            text=f"line-{label}",
            turn_id=f"turn-{label}",
            ts_ms=ts,
        )

    threads = [
        threading.Thread(target=worker, args=("caller", 10)),
        threading.Thread(target=worker, args=("party-2", 20)),
        threading.Thread(target=worker, args=("party-3", 30)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    out = store.get_transcript(parent)
    assert out is not None
    assert len(out["entries"]) == 3
    texts = {e["text"] for e in out["entries"]}
    assert texts == {"line-caller", "line-party-2", "line-party-3"}


def test_get_endpoint_returns_404_when_missing():
    client = TestClient(app)
    resp = client.get("/v1/conference/missing-parent/transcript")
    assert resp.status_code == 404


def test_get_endpoint_returns_merged_json():
    parent = "api-test-parent"
    append_tap_turn(
        parent_session_uuid=parent,
        speaker_label="caller",
        text="api line",
        turn_id="t1",
        ts_ms=42,
    )
    finalize_conference(parent)
    client = TestClient(app)
    resp = client.get(f"/v1/conference/{parent}/transcript")
    assert resp.status_code == 200
    body = resp.json()
    assert body["entries"][0]["speaker"] == "caller"
    assert body["formatted"] == "caller: api line"
    assert body["finalized"] is True
