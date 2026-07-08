"""Prompt-mode push paths must append to LLM history (moderator loop fix)."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest
from starlette.testclient import TestClient

from app.clients import orchestrator
from app.config import tenant_config
from app.engine import prompt_agent
from app.main import app
from app.ws import handler as ws_handler
from tests.unit.test_prompt_ws_integration import (
    ScriptedLLM,
    _collect_push,
    _drive_turn,
    _receive_json_timeout,
    _send_playback_done,
    _start_session_with_tenant_id,
    _wait_for,
)


@pytest.fixture(autouse=True)
def _clean_prompt_state(monkeypatch):
    monkeypatch.setenv("CONFERENCE_THIRD_PARTY_NUMBER", "9810319857")
    monkeypatch.setenv("CONFERENCE_CALLER_ID", "1725617003")
    prompt_agent.reset_state()
    yield
    prompt_agent.reset_state()


def test_conference_join_success_push_updates_history_and_next_turn_no_rejoin(
    monkeypatch,
):
    """Watcher success push must land in history; follow-up turn must not re-dial."""
    monkeypatch.setattr(ws_handler, "CONFERENCE_JOIN_PUSH_POLL_S", 0.05)
    monkeypatch.setattr(ws_handler, "derive_conference_join_push_budget_s", lambda **_kw: 2.0)

    allow_up = threading.Event()

    def status(**kw: Any) -> dict[str, Any]:
        if not allow_up.is_set():
            return {"status": "ringing", "conference_id": "conf-live"}
        return {"status": "up", "conference_id": "conf-live"}

    monkeypatch.setattr(
        orchestrator,
        "conference_join",
        lambda **kw: {"conference_id": "conf-live", "status": "joining"},
    )
    monkeypatch.setattr(orchestrator, "conference_join_status", status)

    llm = ScriptedLLM()
    llm.replies = [
        "Connect kar raha hoon, ek moment. <conference_join>",
        "Ji, sab theek hai — aap boliye.",
    ]

    with TestClient(app) as client:
        app.state.llm = llm
        with client.websocket_connect("/ws/brain") as ws:
            _start_session_with_tenant_id(ws, "sess-hist", "conference")
            _drive_turn(ws, "sess-hist", "t-join", "third party connect karo")
            _send_playback_done(ws, "sess-hist", "t-join")
            _wait_for(
                lambda: prompt_agent.has_pending_conference_join("sess-hist"),
                what="conference join start",
            )

            allow_up.set()
            push = _collect_push(ws)
            assert push["done"]["disposition"] == "CONFERENCE_JOIN_UP"
            success_line = push["reply"]

            hist = prompt_agent.session_history("sess-hist")
            assert any(
                e["role"] == "system" and "status=up" in e["text"] for e in hist
            )
            assert any(
                e["role"] == "assistant" and success_line.strip() in e["text"]
                for e in hist
            )

            out = _drive_turn(ws, "sess-hist", "t-after", "haan sunai de raha hai")
            assert "<conference_join" not in out["reply"].lower()
            assert llm.calls, "follow-up turn must call LLM"
            last_user = llm.calls[-1]["user"]
            assert "CONFERENCE JOIN RESULT: status=up" in last_user
            assert success_line.strip() in last_user
            assert "connect ho gaye" in last_user.lower()

            ws.send_json({"type": "session_end", "session_id": "sess-hist"})


def test_conference_join_failure_push_updates_history(monkeypatch):
    monkeypatch.setattr(ws_handler, "CONFERENCE_JOIN_PUSH_POLL_S", 0.05)
    monkeypatch.setattr(ws_handler, "derive_conference_join_push_budget_s", lambda **_kw: 0.2)

    monkeypatch.setattr(
        orchestrator,
        "conference_join",
        lambda **kw: {"conference_id": "conf-fail", "status": "joining"},
    )
    monkeypatch.setattr(
        orchestrator,
        "conference_join_status",
        lambda **kw: {"status": "joining", "conference_id": "conf-fail"},
    )

    llm = ScriptedLLM()
    llm.replies = ["Connect kar raha hoon. <conference_join>"]

    with TestClient(app) as client:
        app.state.llm = llm
        with client.websocket_connect("/ws/brain") as ws:
            _start_session_with_tenant_id(ws, "sess-fail-h", "conference")
            _drive_turn(ws, "sess-fail-h", "t-1", "third party add karo")
            _send_playback_done(ws, "sess-fail-h", "t-1")
            _wait_for(
                lambda: prompt_agent.has_pending_conference_join("sess-fail-h"),
                what="conference join start",
            )

            push = _collect_push(ws)
            assert push["done"]["disposition"] == "CONFERENCE_JOIN_FAILED"

            hist = prompt_agent.session_history("sess-fail-h")
            assert any(
                e["role"] == "system" and "status=failed" in e["text"] for e in hist
            )
            assert any(
                e["role"] == "assistant" and "connect nahi kar paya" in e["text"].lower()
                for e in hist
            )

            ws.send_json({"type": "session_end", "session_id": "sess-fail-h"})


@pytest.mark.asyncio
async def test_build_consult_relay_appends_history():
    """Audit: consult watcher calls build_consult_relay which already writes history."""
    class _LLM:
        is_stub = True

        async def complete(self, system: str, user: str, **kw: Any) -> str:
            return "Aapki booking confirm ho gayi hai."

    session = type(
        "S",
        (),
        {
            "session_id": "sess-relay-audit",
            "tenant_id": "booking-confirm",
            "agent_id": "persona_customer",
            "borrower_context": {},
        },
    )()
    reply = await prompt_agent.build_consult_relay(
        session=session,
        llm=_LLM(),
        tenant_cfg=tenant_config("booking-confirm"),
        result={"confirmed": "yes", "note": "ok"},
    )
    assert "confirm ho gayi" in reply.lower()
    hist = prompt_agent.session_history("sess-relay-audit")
    assert any(e["role"] == "system" and "CONSULT RESULT" in e["text"] for e in hist)
    assert any(e["role"] == "assistant" and "confirm ho gayi" in e["text"].lower() for e in hist)


def test_append_push_assistant_history_unit():
    prompt_agent.append_push_assistant_history(
        "unit-sess",
        "Third party connect ho gaye hain.",
        system_text="[CONFERENCE JOIN RESULT: status=up]",
    )
    hist = prompt_agent.session_history("unit-sess")
    assert hist == [
        {"role": "system", "text": "[CONFERENCE JOIN RESULT: status=up]"},
        {"role": "assistant", "text": "Third party connect ho gaye hain."},
    ]
