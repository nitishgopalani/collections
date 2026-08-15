"""W4-2 graceful drain + W4-1 B2 bypass tripwire."""

from __future__ import annotations

from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.engine.dialer_controls import get_controls, reset_controls
from app.engine.dialer_watchdog import maybe_flag_bypass
from app.engine.drain import get_drain
from app.main import app, lifespan
from app.ws.tenant_limits import SESSION_REGISTRY

DAY = date(2026, 8, 15)


@pytest.fixture(autouse=True)
def _reset(tmp_path, monkeypatch):
    monkeypatch.setenv("EXPORTS_DIR", str(tmp_path))
    monkeypatch.setenv("DRAIN_CAP_S", "1")
    get_settings.cache_clear()
    get_drain().reset()
    reset_controls()
    SESSION_REGISTRY.reset()
    yield
    get_drain().reset()
    reset_controls()
    SESSION_REGISTRY.reset()
    get_settings.cache_clear()


def test_drain_rejects_new_turn_allows_inflight(caplog):
    drain = get_drain()
    SESSION_REGISTRY.try_acquire("paisalo", 10)
    drain.begin(cap_s=1)
    assert drain.draining
    assert drain.in_flight() == 1
    SESSION_REGISTRY.release("paisalo")
    assert drain.wait_idle(poll_s=0.01) is True
    assert "drain_started" in caplog.text
    assert "drain_complete" in caplog.text


@pytest.mark.asyncio
async def test_http_turn_rejected_while_draining():
    get_drain().begin(cap_s=1)
    async with lifespan(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/turn",
                json={
                    "call_id": "drain-new",
                    "tenant_id": "default",
                    "borrower_id": "b1",
                    "transcript": "hello",
                },
            )
            assert resp.status_code == 503
            assert resp.json()["detail"] == "draining"


def test_bypass_tripwire_flags_ungated_outbound(caplog):
    flagged = maybe_flag_bypass(
        session_id="sess-rogue",
        channel_id="PJSIP/9810587857-0001",
        borrower_id="b-rogue",
        phone="9810587857",
        borrower_context={"phone": "9810587857"},
        day=DAY,
    )
    assert flagged is True
    assert "dialer_bypass_detected" in caplog.text
    assert "PJSIP/9810587857-0001" in caplog.text


def test_bypass_skips_inbound_and_gated_dial(tmp_path: Path, caplog):
    inbound = maybe_flag_bypass(
        session_id="sess-in",
        channel_id="in-1",
        borrower_id="b-in",
        phone="9810000000",
        borrower_context={"direction": "inbound"},
        day=DAY,
    )
    assert inbound is False

    get_controls().commit_originate(borrower_id="b-ok", phone="9822222222", day=DAY)
    gated = maybe_flag_bypass(
        session_id="sess-ok",
        channel_id="out-1",
        borrower_id="b-ok",
        phone="9822222222",
        borrower_context={"phone": "9822222222"},
        day=DAY,
    )
    assert gated is False
    assert "dialer_bypass_detected" not in caplog.text
