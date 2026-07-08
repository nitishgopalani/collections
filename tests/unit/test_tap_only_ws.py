"""CF2.2 tap_only sessions: transcript-only listeners, no LLM/TTS/actions."""

from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.testclient import TestClient

from app.clients import orchestrator
from app.engine import prompt_agent
from app.main import app
from tests.unit.test_prompt_ws_integration import ScriptedLLM, _drive_turn


@pytest.fixture(autouse=True)
def _clean_prompt_state():
    prompt_agent.reset_state()
    yield
    prompt_agent.reset_state()


def _start_tap_session(
    ws,
    session_id: str,
    *,
    speaker_label: str = "caller",
    parent_session_uuid: str = "parent-main-uuid",
) -> None:
    ws.send_json(
        {
            "type": "session_start",
            "session_id": session_id,
            "borrower_id": "tap-1",
            "agent_id": "conference",
            "client_id": "conference",
            "borrower_context": {
                "speaker_label": speaker_label,
                "tap_only": True,
                "parent_session_uuid": parent_session_uuid,
            },
        }
    )
    ready = json.loads(ws.receive_text())
    assert ready["type"] == "session_ready"


def test_tap_only_session_start_ctx_keys(monkeypatch):
    monkeypatch.setattr(app.state, "llm", ScriptedLLM())
    with TestClient(app).websocket_connect("/ws/brain") as ws:
        _start_tap_session(ws, "tap-caller-1", speaker_label="party-2")
        # session_start logged ctx_keys include tap metadata — exercise a turn next.
        out = _drive_turn(ws, "tap-caller-1", "t1", "ek do teen")
        assert out["reply"] == ""
        assert "chunk" not in out["types"]


def test_tap_only_turn_never_calls_llm_or_orchestrator(monkeypatch):
    llm = ScriptedLLM()
    llm.replies = ["Should never be spoken."]
    monkeypatch.setattr(app.state, "llm", llm)

    join_calls: list[dict[str, Any]] = []

    def fake_join(**kwargs):
        join_calls.append(kwargs)
        return {"conference_id": "conf-x", "status": "joining"}

    monkeypatch.setattr(orchestrator, "conference_join", fake_join)

    with TestClient(app).websocket_connect("/ws/brain") as ws:
        _start_tap_session(ws, "tap-silent-1")
        out = _drive_turn(ws, "tap-silent-1", "t-opener", "")
        assert out["reply"] == ""
        assert llm.calls == []
        assert join_calls == []

        out2 = _drive_turn(ws, "tap-silent-1", "t-speech", "char paanch chhe")
        assert out2["reply"] == ""
        assert llm.calls == []
        assert join_calls == []


def test_tap_only_playback_done_is_ignored(monkeypatch):
    """tap_only must not start deferred conference_join on playback_done."""
    llm = ScriptedLLM()
    monkeypatch.setattr(app.state, "llm", llm)
    join_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        orchestrator,
        "conference_join",
        lambda **kw: join_calls.append(kw) or {"conference_id": "conf-y", "status": "joining"},
    )

    with TestClient(app).websocket_connect("/ws/brain") as ws:
        _start_tap_session(ws, "tap-pbd-1")
        ws.send_json(
            {
                "type": "playback_done",
                "session_id": "tap-pbd-1",
                "turn_id": "ghost-turn",
            }
        )
        # No pushed reply / done from playback_done handling.
        ws.send_json(
            {
                "type": "turn",
                "session_id": "tap-pbd-1",
                "turn_id": "t-after",
                "transcript": "hello",
                "flow_class": "Default",
            }
        )
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "flow_class"
        done = json.loads(ws.receive_text())
        assert done["type"] == "done"
        assert join_calls == []
        assert llm.calls == []
