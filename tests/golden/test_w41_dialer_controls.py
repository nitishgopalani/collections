"""W4-1 dialer controls.

  - seeded DNC row → originate refused (dnc_suppressed)
  - 3rd attempt same day → cadence_blocked
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.engine.dialer_controls import DialerControls, get_controls, reset_controls
from app.engine.obligation_export import export_closed_call, reset_webhook_stub
from app.main import app
from app.schemas.api import TurnRequest
from app.schemas.state import ConversationState

DAY = date(2026, 8, 15)
STAMP = "20260815"


@pytest.fixture
def exports(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("EXPORTS_DIR", str(tmp_path))
    monkeypatch.setenv("DIALER_MAX_ATTEMPTS_PER_DAY", "2")
    monkeypatch.setenv("DIALER_GATE_ENABLED", "true")
    get_settings.cache_clear()
    reset_controls()
    reset_webhook_stub()
    yield tmp_path
    reset_controls()
    get_settings.cache_clear()


def test_seeded_dnc_row_refuses_originate(exports: Path, caplog):
    controls = DialerControls()
    controls.record_dnc(borrower_id="b-dnc", phone="9810587857", source="seed")
    decision = controls.check_originate(
        borrower_id="b-dnc",
        phone="+91-98105-87857",
        day=DAY,
        max_attempts=2,
    )
    assert decision.allow is False
    assert decision.reason == "dnc_suppressed"
    assert "dnc_suppressed" in caplog.text


def test_dnc_from_w33_disposition_export(exports: Path):
    state = ConversationState(
        call_id="w41-dnc-export",
        borrower_id="b-export-dnc",
        tenant_id="paisalo",
        slots={
            "disposition": "dnc_requested",
            "call_date": DAY.isoformat(),
            "plo_scenario": "predue",
        },
    )
    req = TurnRequest(
        call_id=state.call_id,
        borrower_id=state.borrower_id,
        tenant_id="paisalo",
        transcript="dobara call mat karna",
        turn_meta={"call_date": DAY.isoformat()},
    )
    export_closed_call(state, req, last_transcript="dobara call mat karna")
    decision = get_controls().check_originate(
        borrower_id="b-export-dnc",
        day=DAY,
        max_attempts=2,
    )
    assert decision.allow is False
    assert decision.reason == "dnc_suppressed"


def test_third_attempt_same_day_blocked(exports: Path):
    controls = DialerControls()
    first = controls.commit_originate(borrower_id="b-cadence", phone="9000000001", day=DAY)
    controls.release(borrower_id="b-cadence", phone="9000000001")
    second = controls.commit_originate(borrower_id="b-cadence", phone="9000000001", day=DAY)
    controls.release(borrower_id="b-cadence", phone="9000000001")
    assert first.allow and second.allow
    assert second.attempts_today == 2
    third = controls.check_originate(borrower_id="b-cadence", phone="9000000001", day=DAY)
    assert third.allow is False
    assert third.reason == "cadence_blocked"
    assert third.attempts_today == 2


def test_active_call_lock(exports: Path):
    controls = DialerControls()
    ok = controls.commit_originate(borrower_id="b-lock", phone="9000000002", day=DAY)
    assert ok.allow
    locked = controls.check_originate(borrower_id="b-lock", phone="9000000002", day=DAY)
    assert locked.reason == "active_call"
    controls.release(borrower_id="b-lock", phone="9000000002")
    after = controls.check_originate(borrower_id="b-lock", phone="9000000002", day=DAY)
    assert after.allow is True


def test_callback_consume_skips_dnc(exports: Path):
    controls = DialerControls()
    controls.record_dnc(borrower_id="cb-dnc", source="seed")
    cb = exports / f"callbacks_{STAMP}.jsonl"
    cb.write_text(
        '{"borrower_id":"cb-ok","phone":"9000000003","disposition":"callback_request"}\n'
        '{"borrower_id":"cb-dnc","phone":"9000000004","disposition":"callback_request"}\n',
        encoding="utf-8",
    )
    out = controls.consume_callbacks(DAY, max_attempts=2, commit=False)
    due_ids = {r["borrower_id"] for r in out["due"]}
    skipped = {r["borrower_id"]: r["gate"]["reason"] for r in out["skipped"]}
    assert due_ids == {"cb-ok"}
    assert skipped["cb-dnc"] == "dnc_suppressed"


@pytest.mark.asyncio
async def test_http_dnc_and_cadence(exports: Path):
    get_controls().record_dnc(borrower_id="http-dnc", phone="9111111111")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        banned = await client.post(
            "/dialer/v0/originate",
            json={"borrower_id": "http-dnc", "phone": "9111111111", "day": DAY.isoformat()},
        )
        assert banned.status_code == 403
        assert banned.json()["detail"]["reason"] == "dnc_suppressed"

        a = await client.post(
            "/dialer/v0/originate",
            json={"borrower_id": "http-cad", "phone": "9222222222", "day": DAY.isoformat()},
        )
        await client.post(
            "/dialer/v0/complete",
            json={"borrower_id": "http-cad", "phone": "9222222222"},
        )
        b = await client.post(
            "/dialer/v0/originate",
            json={"borrower_id": "http-cad", "phone": "9222222222", "day": DAY.isoformat()},
        )
        await client.post(
            "/dialer/v0/complete",
            json={"borrower_id": "http-cad", "phone": "9222222222"},
        )
        assert a.status_code == 200 and b.status_code == 200
        c = await client.post(
            "/dialer/v0/originate",
            json={"borrower_id": "http-cad", "phone": "9222222222", "day": DAY.isoformat()},
        )
        assert c.status_code == 429
        assert c.json()["detail"]["reason"] == "cadence_blocked"
