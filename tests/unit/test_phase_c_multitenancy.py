"""Phase C — multi-tenancy: tenant resolution, per-tenant defaults, isolation.

C1: client_id threads a tenant into TurnRequest.tenant_id; no client_id falls
    back to the pre-Phase-C chain (backward compatible).
C2: two client_ids route to two different pack/locale defaults when omitted;
    explicit session_start values override the tenant mapping.
C3: per-tenant concurrency cap rejects the over-cap session; the counter is
    released on disconnect and never goes negative.
"""

import json
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import get_settings
from app.main import app
from app.schemas.api import TurnResponse
from app.ws.routing import resolve_session_defaults, resolve_session_tenant
from app.ws.tenant_limits import SESSION_REGISTRY, TenantSessionRegistry

# ---------------------------------------------------------------------------
# Pure resolver unit tests (fast, deterministic)
# ---------------------------------------------------------------------------


def test_resolve_tenant_client_id_wins():
    tenant, source = resolve_session_tenant(
        client_id="acme",
        routed_tenant="salary_on_time",
        inbound_tenant_id="something",
        default_tenant_id="default",
    )
    assert (tenant, source) == ("acme", "client_id")


def test_resolve_tenant_falls_back_to_prior_chain():
    # No client_id -> exact pre-Phase-C behaviour: routed -> tenant_id -> default.
    assert resolve_session_tenant(
        client_id="", routed_tenant="salary_on_time", inbound_tenant_id=None,
        default_tenant_id="default",
    ) == ("salary_on_time", "agent_routing")
    assert resolve_session_tenant(
        client_id="", routed_tenant=None, inbound_tenant_id="acme",
        default_tenant_id="default",
    ) == ("acme", "session_tenant_id")
    assert resolve_session_tenant(
        client_id="", routed_tenant=None, inbound_tenant_id=None,
        default_tenant_id="default",
    ) == ("default", "default")


def test_resolve_defaults_gap_fill_and_override():
    # Gap-fill from tenant defaults when caller omits.
    assert resolve_session_defaults(
        default_pack_id="p", default_agent_id="a", default_locale="en-IN",
        explicit_pack_id="", explicit_agent_id="", explicit_locale="",
    ) == ("p", "a", "en-IN")
    # Explicit values win.
    assert resolve_session_defaults(
        default_pack_id="p", default_agent_id="a", default_locale="en-IN",
        explicit_pack_id="P2", explicit_agent_id="A2", explicit_locale="mr-IN",
    ) == ("P2", "A2", "mr-IN")
    # locale falls back to hi-IN when neither is set.
    assert resolve_session_defaults(
        default_pack_id="", default_agent_id="", default_locale="",
        explicit_pack_id="", explicit_agent_id="", explicit_locale="",
    ) == ("", "", "hi-IN")


# ---------------------------------------------------------------------------
# Registry unit tests (C3 counter semantics)
# ---------------------------------------------------------------------------


def test_registry_cap_and_release():
    reg = TenantSessionRegistry()
    assert reg.try_acquire("t", cap=2) is True
    assert reg.try_acquire("t", cap=2) is True
    assert reg.try_acquire("t", cap=2) is False  # at cap
    reg.release("t")
    assert reg.try_acquire("t", cap=2) is True  # slot freed


def test_registry_unlimited_when_cap_zero():
    reg = TenantSessionRegistry()
    for _ in range(100):
        assert reg.try_acquire("t", cap=0) is True


def test_registry_never_negative():
    reg = TenantSessionRegistry()
    reg.release("t")  # release without acquire
    reg.release("t")
    assert reg.active("t") == 0
    reg.try_acquire("t", cap=0)
    reg.release("t")
    reg.release("t")  # extra release
    assert reg.active("t") == 0


# ---------------------------------------------------------------------------
# Handler integration tests (C1/C2/C3) via the real WS route
# ---------------------------------------------------------------------------


@pytest.fixture
def non_test_settings(monkeypatch):
    """Force a clean non-TEST_MODE settings cache for the handler under test."""
    monkeypatch.setenv("TEST_MODE", "false")
    monkeypatch.setenv("DEFAULT_TENANT_ID", "default")
    get_settings.cache_clear()
    SESSION_REGISTRY.reset()
    yield
    get_settings.cache_clear()
    SESSION_REGISTRY.reset()


def _capture_turn_request(monkeypatch) -> dict:
    captured: dict = {}

    async def fake_handle_turn(*args, **kwargs):
        captured["request"] = args[0]
        return TurnResponse(reply_text="ok")

    monkeypatch.setattr("app.ws.handler.handle_turn", AsyncMock(side_effect=fake_handle_turn))
    return captured


def _run_one_turn(ws, *, session_id="s1", extra_start=None):
    start = {
        "type": "session_start",
        "session_id": session_id,
        "borrower_id": "b-1",
        "agent_id": "agent-generic",
    }
    if extra_start:
        start.update(extra_start)
    ws.send_json(start)
    # session_ready ack
    ws.receive_text()
    ws.send_json({"type": "turn", "session_id": session_id, "turn_id": "t1", "transcript": "hi"})
    for _ in range(20):
        msg = json.loads(ws.receive_text())
        if msg["type"] == "done":
            break


def test_c1_client_id_threads_tenant(non_test_settings, monkeypatch):
    captured = _capture_turn_request(monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/brain") as ws:
            _run_one_turn(ws, extra_start={"client_id": "acme_collections"})
    assert captured["request"].tenant_id == "acme_collections"


def test_c1_no_client_id_falls_back_to_default(non_test_settings, monkeypatch):
    captured = _capture_turn_request(monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/brain") as ws:
            _run_one_turn(ws)  # no client_id
    assert captured["request"].tenant_id == "default"


def test_c2_tenant_defaults_fill_pack_and_locale(non_test_settings, monkeypatch):
    captured = _capture_turn_request(monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/brain") as ws:
            _run_one_turn(ws, extra_start={"client_id": "acme_collections"})
    req = captured["request"]
    assert req.pack_id == "acme_default_pack"
    assert req.locale == "en-IN"


def test_c2_second_tenant_gets_different_defaults(non_test_settings, monkeypatch):
    captured = _capture_turn_request(monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/brain") as ws:
            _run_one_turn(ws, extra_start={"client_id": "globex_recoveries"})
    req = captured["request"]
    assert req.pack_id == "globex_default_pack"
    assert req.locale == "ta-IN"


def test_c2_explicit_values_override_tenant_defaults(non_test_settings, monkeypatch):
    captured = _capture_turn_request(monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/brain") as ws:
            _run_one_turn(
                ws,
                extra_start={
                    "client_id": "acme_collections",
                    "pack_id": "explicit_pack",
                    "locale": "mr-IN",
                },
            )
    req = captured["request"]
    assert req.pack_id == "explicit_pack"
    assert req.locale == "mr-IN"


def test_c3_over_cap_session_rejected(non_test_settings, monkeypatch):
    # smallco_pilot has max_concurrent_sessions=1 in config.
    _capture_turn_request(monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/brain") as first:
            first.send_json({
                "type": "session_start", "session_id": "s-a", "borrower_id": "b",
                "agent_id": "agent-generic", "client_id": "smallco_pilot",
            })
            first.receive_text()  # session_ready -> slot held

            # Second concurrent session for the same tenant must be rejected/closed.
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect("/ws/brain") as second:
                    second.send_json({
                        "type": "session_start", "session_id": "s-b", "borrower_id": "b",
                        "agent_id": "agent-generic", "client_id": "smallco_pilot",
                    })
                    second.receive_text()


def test_c3_slot_freed_after_session_ends(non_test_settings, monkeypatch):
    _capture_turn_request(monkeypatch)
    with TestClient(app) as client:
        # First session acquires then ends cleanly, releasing the slot.
        with client.websocket_connect("/ws/brain") as first:
            first.send_json({
                "type": "session_start", "session_id": "s-a", "borrower_id": "b",
                "agent_id": "agent-generic", "client_id": "smallco_pilot",
            })
            first.receive_text()
            first.send_json({"type": "session_end", "session_id": "s-a"})
        # A new session for the same capped tenant is now accepted.
        with client.websocket_connect("/ws/brain") as second:
            second.send_json({
                "type": "session_start", "session_id": "s-b", "borrower_id": "b",
                "agent_id": "agent-generic", "client_id": "smallco_pilot",
            })
            ready = json.loads(second.receive_text())
            assert ready["type"] == "session_ready"
    assert SESSION_REGISTRY.active("smallco_pilot") == 0
