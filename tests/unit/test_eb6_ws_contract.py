"""EB-6 WebSocket contract tests."""

import json

import pytest
from starlette.testclient import TestClient

from app.main import app
from app.ws.chunking import chunk_reply_for_tts
from app.ws.flow_class import flow_class_for_question_slot


def test_chunk_reply_splits_sentences():
    text = "Namaste. Main aapki madad kar sakta hoon. Kya aap sun pa rahe hain?"
    chunks = chunk_reply_for_tts(text)
    assert len(chunks) == 3
    assert chunks[0].startswith("Namaste")


def test_flow_class_mapping():
    assert flow_class_for_question_slot("identity_response") == "SpelledInput"
    assert flow_class_for_question_slot("third_party_borrower_check") == "YesNo"
    assert flow_class_for_question_slot("ptp_date") == "Default"
    assert flow_class_for_question_slot(None) == "Default"


def test_brain_ws_turn_emits_chunk_flow_class_done():
    with TestClient(app) as client:
        with client.websocket_connect("/ws/brain") as ws:
            ws.send_json(
                {
                    "type": "session_start",
                    "session_id": "sess-eb6-1",
                    "borrower_id": "bor-1",
                    "agent_id": "agent-1",
                    "pack_id": "pack-1",
                    "locale": "hi-IN",
                }
            )
            ws.send_json(
                {
                    "type": "turn",
                    "session_id": "sess-eb6-1",
                    "turn_id": "turn-1",
                    "transcript": "kal payment kar dunga",
                    "flow_class": "Default",
                }
            )

            outbound_types: list[str] = []
            chunk_seqs: list[int] = []
            for _ in range(20):
                raw = ws.receive_text()
                msg = json.loads(raw)
                outbound_types.append(msg["type"])
                if msg["type"] == "chunk":
                    assert msg["turn_id"] == "turn-1"
                    assert msg["text"]
                    chunk_seqs.append(msg["seq"])
                if msg["type"] == "flow_class":
                    assert msg["next"] in {"YesNo", "Default", "SpelledInput"}
                if msg["type"] == "done":
                    assert msg["turn_id"] == "turn-1"
                    assert "audit_id" in msg
                    break
                if msg["type"] == "error":
                    pytest.fail(msg.get("fallback_text", "brain ws error"))
            else:
                pytest.fail(f"expected done message, got: {outbound_types}")

            assert "chunk" in outbound_types
            assert chunk_seqs and chunk_seqs[0] == 0
            assert "flow_class" in outbound_types
            assert outbound_types[-1] == "done"

            ws.send_json({"type": "session_end", "session_id": "sess-eb6-1"})


@pytest.mark.asyncio
async def test_brain_ws_cancel_marks_session():
    from app.ws.session import BrainWSSession

    session = BrainWSSession(
        session_id="sess",
        borrower_id="b",
        agent_id="a",
    )
    session.register_turn("turn-cancel")
    session.cancel_turn("turn-cancel")
    assert session.is_cancelled("turn-cancel")


def test_brain_ws_opener_allows_empty_transcript():
    with TestClient(app) as client:
        with client.websocket_connect("/ws/brain") as ws:
            ws.send_json(
                {
                    "type": "session_start",
                    "session_id": "sess-opener",
                    "borrower_id": "bor-opener",
                    "agent_id": "agent-1",
                }
            )
            ws.send_json(
                {
                    "type": "turn",
                    "session_id": "sess-opener",
                    "turn_id": "turn-opener",
                    "transcript": "",
                    "flow_class": "Default",
                }
            )
            saw_done = False
            for _ in range(10):
                raw = ws.receive_text()
                msg = json.loads(raw)
                if msg["type"] == "done":
                    saw_done = True
                    break
            assert saw_done

            ws.send_json({"type": "session_end", "session_id": "sess-opener"})
