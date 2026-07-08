"""CF1.5 conference moderator — marker, deferred join, status-driven speech."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest
from starlette.testclient import TestClient

from app.clients import orchestrator
from app.config import get_settings, tenant_config
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
def _conference_join_env(monkeypatch):
    monkeypatch.setenv("CONFERENCE_THIRD_PARTY_NUMBER", "9810319857")
    monkeypatch.setenv("CONFERENCE_CALLER_ID", "1725617003")
    prompt_agent.reset_state()
    yield
    prompt_agent.reset_state()


@pytest.mark.asyncio
async def test_conference_join_marker_deferred_not_dialled_inline(monkeypatch):
    joined: list[dict[str, Any]] = []

    def fake_join(**kwargs: Any) -> dict[str, Any]:
        joined.append(kwargs)
        return {
            "conference_id": "conf-1",
            "status": "joining",
            "bridge_id": "b-1",
        }

    monkeypatch.setattr(orchestrator, "conference_join", fake_join)
    monkeypatch.setenv("CONFERENCE_THIRD_PARTY_NUMBER", "9810319857")
    monkeypatch.setenv("CONFERENCE_CALLER_ID", "1725617003")

    class _LLM:
        is_stub = True

        async def complete(self, system: str, user: str, **kw: Any) -> str:
            return "Connect kar raha hoon, ek moment. <conference_join>"

    session = type(
        "S",
        (),
        {
            "session_id": "sess-cj",
            "tenant_id": "conference",
            "agent_id": "default",
            "borrower_context": {},
        },
    )()
    tenant_cfg = tenant_config("conference")
    out = await prompt_agent.handle_prompt_turn(
        session=session,
        transcript="third party connect karo",
        llm=_LLM(),
        tenant_cfg=tenant_cfg,
    )
    assert "<conference_join" not in out.reply_text.lower()
    assert out.conference_join_request is not None
    assert joined == []
    assert not prompt_agent.has_pending_conference_join("sess-cj")

    ok = await prompt_agent.start_deferred_conference_join(session)
    assert ok
    assert joined == [
        {
            "session_uuid": "sess-cj",
            "to": "9810319857",
            "caller_id": "1725617003",
            "ring_budget_s": get_settings().conference_join_ring_budget_s,
        }
    ]
    assert prompt_agent.has_pending_conference_join("sess-cj")


def test_conference_join_watcher_announces_only_on_up(monkeypatch):
    monkeypatch.setattr(ws_handler, "CONFERENCE_JOIN_PUSH_POLL_S", 0.05)
    monkeypatch.setattr(ws_handler, "derive_conference_join_push_budget_s", lambda **_kw: 2.0)

    allow_up = threading.Event()

    def status(**kw: Any) -> dict[str, Any]:
        if not allow_up.is_set():
            return {"status": "ringing", "conference_id": "conf-live"}
        return {"status": "up", "conference_id": "conf-live"}

    joined: list[dict[str, Any]] = []
    monkeypatch.setattr(
        orchestrator,
        "conference_join",
        lambda **kw: joined.append(kw)
        or {"conference_id": "conf-live", "status": "joining"},
    )
    monkeypatch.setattr(orchestrator, "conference_join_status", status)

    llm = ScriptedLLM()
    llm.replies = ["Connect kar raha hoon, ek moment. <conference_join>"]

    with TestClient(app) as client:
        app.state.llm = llm
        with client.websocket_connect("/ws/brain") as ws:
            _start_session_with_tenant_id(ws, "sess-up", "conference")
            out = _drive_turn(ws, "sess-up", "t-join", "third party connect karo")
            assert "<conference_join" not in out["reply"].lower()
            assert joined == []
            _send_playback_done(ws, "sess-up", "t-join")
            _wait_for(lambda: bool(joined), what="conference_join API")
            assert prompt_agent.has_pending_conference_join("sess-up")

            # Status stays ringing until we release — no premature success push.
            time.sleep(0.25)
            silent = _receive_json_timeout(ws, 0.15)
            assert silent is None

            allow_up.set()
            push = _collect_push(ws)
            assert push["done"]["disposition"] == "CONFERENCE_JOIN_UP"
            assert "connect ho gaye" in push["reply"].lower()
            assert not prompt_agent.has_pending_conference_join("sess-up")

            ws.send_json({"type": "session_end", "session_id": "sess-up"})


def test_conference_join_watcher_announces_failure(monkeypatch):
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
            _start_session_with_tenant_id(ws, "sess-fail", "conference")
            _drive_turn(ws, "sess-fail", "t-1", "third party add karo")
            _send_playback_done(ws, "sess-fail", "t-1")
            _wait_for(
                lambda: prompt_agent.has_pending_conference_join("sess-fail"),
                what="conference join start",
            )

            push = _collect_push(ws)
            assert push["done"]["disposition"] == "CONFERENCE_JOIN_FAILED"
            assert "connect nahi kar paya" in push["reply"].lower()

            ws.send_json({"type": "session_end", "session_id": "sess-fail"})


def test_no_success_announcement_while_joining_status(monkeypatch):
    """Prove the watcher does NOT push success while status is joining/ringing."""
    monkeypatch.setattr(ws_handler, "CONFERENCE_JOIN_PUSH_POLL_S", 0.05)
    monkeypatch.setattr(ws_handler, "derive_conference_join_push_budget_s", lambda **_kw: 5.0)

    monkeypatch.setattr(
        orchestrator,
        "conference_join",
        lambda **kw: {"conference_id": "conf-wait", "status": "joining"},
    )
    monkeypatch.setattr(
        orchestrator,
        "conference_join_status",
        lambda **kw: {"status": "joining"},
    )

    llm = ScriptedLLM()
    llm.replies = ["Ek moment. <conference_join>"]

    with TestClient(app) as client:
        app.state.llm = llm
        with client.websocket_connect("/ws/brain") as ws:
            _start_session_with_tenant_id(ws, "sess-wait", "conference")
            _drive_turn(ws, "sess-wait", "t-1", "connect third party")
            _send_playback_done(ws, "sess-wait", "t-1")
            _wait_for(
                lambda: prompt_agent.has_pending_conference_join("sess-wait"),
                what="join pending",
            )
            time.sleep(0.25)
            msg = _receive_json_timeout(ws, 0.15)
            assert msg is None, "must not push success while still joining"
            ws.send_json({"type": "session_end", "session_id": "sess-wait"})
