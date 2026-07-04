"""Tests for gated reply streaming over EB-6 brain websocket."""

import json
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from app.main import app
from app.schemas.api import TurnResponse


def test_brain_ws_streams_gated_chunks_before_done(monkeypatch):
    async def run_with_callback(*args, **kwargs):
        cb = kwargs.get("on_gated_reply")
        assert cb is not None, "brain ws must invoke on_gated_reply after compliance gate"
        await cb("Pehla. Doosra.")
        return TurnResponse(reply_text="Pehla. Doosra.")

    monkeypatch.setattr("app.ws.handler.handle_turn", AsyncMock(side_effect=run_with_callback))

    with TestClient(app) as client:
        with client.websocket_connect("/ws/brain") as ws:
            ws.send_json(
                {
                    "type": "session_start",
                    "session_id": "sess-stream",
                    "borrower_id": "bor-1",
                    "agent_id": "agent-1",
                }
            )
            ready = json.loads(ws.receive_text())
            assert ready["type"] == "session_ready"
            assert ready["session_id"] == "sess-stream"
            ws.send_json(
                {
                    "type": "turn",
                    "session_id": "sess-stream",
                    "turn_id": "turn-stream",
                    "transcript": "hello",
                    "flow_class": "Default",
                }
            )

            saw_chunk_before_done = False
            chunk_seqs: list[int] = []
            for _ in range(20):
                raw = ws.receive_text()
                msg = json.loads(raw)
                if msg["type"] == "chunk":
                    saw_chunk_before_done = True
                    chunk_seqs.append(msg["seq"])
                if msg["type"] == "done":
                    assert saw_chunk_before_done, "chunk must arrive before done (gate-before-speak path)"
                    assert chunk_seqs and chunk_seqs[0] == 0
                    break
            else:
                pytest.fail("expected done after streamed chunks")

            ws.send_json({"type": "session_end", "session_id": "sess-stream"})
