"""W4-4 platform: call_summary line, /version, stub borrower-state smoke."""

from __future__ import annotations

import json
import logging

import pytest
from httpx import ASGITransport, AsyncClient

from app.clients.tools_stub import StubToolClient
from app.config import get_settings
from app.engine.call_summary import emit_call_summary, record_turn, reset_summaries
from app.engine.turn import handle_turn
from app.main import app, lifespan
from app.memory.store import InMemoryMemoryStore
from app.schemas.state import BorrowerRecord, ConversationState
from app.version import build_info
from tests.golden.test_w43_tools_live import (
    BORROWER,
    TENANT,
    _EmptyKB,
    _ScriptedLLM,
    _req,
)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("SCRIPTED_CATALOG_ROUTING", "true")
    monkeypatch.setenv("CALL_WINDOW_START", "00:00")
    monkeypatch.setenv("CALL_WINDOW_END", "23:59")
    monkeypatch.setenv("TEST_PLO_SCENARIO", "postdue3")
    monkeypatch.setenv("COMMITMENT_GATE_ENFORCE", "true")
    monkeypatch.setenv("GIT_SHA", "w44deadbeef")
    monkeypatch.setenv("GIT_BRANCH", "feature/tier23-engine-upgrade")
    get_settings.cache_clear()
    reset_summaries()
    yield
    reset_summaries()
    get_settings.cache_clear()


def test_build_info_reads_env(monkeypatch):
    monkeypatch.setenv("GIT_SHA", "abc123")
    monkeypatch.setenv("GIT_BRANCH", "main")
    info = build_info()
    assert info["git_sha"] == "abc123"
    assert info["git_branch"] == "main"


@pytest.mark.asyncio
async def test_version_endpoint():
    async with lifespan(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["git_sha"] == "w44deadbeef"
    assert body["git_branch"] == "feature/tier23-engine-upgrade"
    assert "build_time" in body
    assert "stale" in body


def test_call_summary_one_json_line(caplog):
    state = ConversationState(
        call_id="w44-sum",
        tenant_id="paisalo",
        borrower_id="b1",
        slots={
            "plo_scenario": "postdue3",
            "disposition": "PTP_SET",
            "ptp_date": "2026-08-20",
            "ptp_amount": 4500,
            "payment_claimed": True,
            "end_call": True,
            "_tool_degraded": True,
        },
    )
    record_turn(state, latency_ms=12.0, llm_calls=0)
    record_turn(state, latency_ms=40.0, llm_calls=1)
    with caplog.at_level(logging.INFO, logger="app.engine.call_summary"):
        first = emit_call_summary("w44-sum")
        second = emit_call_summary("w44-sum")
    assert second is None
    assert first is not None
    assert first["session_id"] == "w44-sum"
    assert first["tenant"] == "paisalo"
    assert first["scenario"] == "postdue3"
    assert first["turns"] == 2
    assert "PTP_SET" in first["dispositions"]
    assert first["ptp_date"] == "2026-08-20"
    assert first["latency_p50_ms"] == 26.0
    assert first["latency_max_ms"] == 40.0
    assert first["llm_free_pct"] == 50.0
    assert first["tool_degraded"] >= 1
    assert "payment_claimed" in first["flags"]
    assert "call_summary" in caplog.text
    payload = json.loads(caplog.text.split("call_summary ", 1)[1].strip().splitlines()[0])
    assert payload["session_id"] == "w44-sum"


@pytest.mark.asyncio
async def test_stub_borrower_state_smoke():
    memory = InMemoryMemoryStore()
    await memory.save_borrower(
        BorrowerRecord(
            borrower_id=BORROWER,
            identity={"name": "रमेश"},
            loan={"amount_due": 4500, "account_ref": "LN-W44", "last_date_paid": "2026-06-01"},
            comms_prefs={"phone": "9810587857"},
        )
    )
    stub = StubToolClient(source=memory)
    result = await stub.get_borrower_state(loan_ref="LN-W44", tenant_id=TENANT)
    assert result["ok"] is True
    assert result["result"]["outstanding"] == 4500
    assert result["result"]["found"] is True


@pytest.mark.asyncio
async def test_ended_turn_emits_summary(caplog):
    memory = InMemoryMemoryStore()
    llm = _ScriptedLLM()
    await handle_turn(
        _req("w44-end", ""),
        memory=memory,
        llm=llm,
        tools=StubToolClient(source=memory),
        kb=_EmptyKB(),
    )
    with caplog.at_level(logging.INFO, logger="app.engine.call_summary"):
        payload = emit_call_summary("w44-end")
    assert payload is not None
    assert payload["turns"] >= 1
    assert payload["tenant"] == TENANT
