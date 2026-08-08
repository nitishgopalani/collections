"""HARDEN-1 / G-A3-01 / G-A2-01 — tenant routing truth.

(a) session_start client_id=paisalo → tenant=paisalo
(b) legacy empty client_id → agent_routing / default chain
(c) TEST_MODE must NOT pin tenant (only TEST_FORCE_TENANT overrides)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.schemas.api import TurnResponse
from app.ws.routing import canonicalize_client_id, resolve_session_tenant
from app.ws.tenant_limits import SESSION_REGISTRY


def test_canonicalize_salary_hyphen():
    assert canonicalize_client_id("salary-on-time") == "salary_on_time"
    assert canonicalize_client_id("paisalo") == "paisalo"
    assert canonicalize_client_id("") == ""


def test_resolve_tenant_paisalo_client_id_wins():
    tenant, source = resolve_session_tenant(
        client_id="paisalo",
        routed_tenant="salary_on_time",
        inbound_tenant_id="default",
        default_tenant_id="default",
    )
    assert (tenant, source) == ("paisalo", "client_id")


def test_f2_client_id_beats_stale_session_tenant_id():
    """HARDEN-1 F2 / G-A3-03: client_id=paisalo + stale tenant_id=salary_on_time
    (injected by go-server for one more release) → paisalo wins, source=client_id.
    The brain must NOT let the legacy injected tenant_id override the connector's
    client_id on the BYO/media-meta path."""
    tenant, source = resolve_session_tenant(
        client_id="paisalo",
        routed_tenant=None,
        inbound_tenant_id="salary_on_time",
        default_tenant_id="default",
    )
    assert (tenant, source) == ("paisalo", "client_id")


def test_resolve_tenant_legacy_no_client_id_uses_agent_routing():
    tenant, source = resolve_session_tenant(
        client_id="",
        routed_tenant="salary_on_time",
        inbound_tenant_id=None,
        default_tenant_id="default",
    )
    assert (tenant, source) == ("salary_on_time", "agent_routing")


def test_resolve_tenant_salary_hyphen_canonicalizes():
    tenant, source = resolve_session_tenant(
        client_id="salary-on-time",
        routed_tenant=None,
        inbound_tenant_id=None,
        default_tenant_id="default",
    )
    assert (tenant, source) == ("salary_on_time", "client_id")


@pytest.fixture
def non_test_settings(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "false")
    monkeypatch.setenv("TEST_FORCE_TENANT", "")
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

    monkeypatch.setattr(
        "app.ws.handler.handle_turn", AsyncMock(side_effect=fake_handle_turn)
    )
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
    ready = json.loads(ws.receive_text())
    assert ready["type"] == "session_ready"
    assert ready["session_id"] == session_id
    ws.send_json(
        {"type": "turn", "session_id": session_id, "turn_id": "t1", "transcript": "hi"}
    )
    for _ in range(20):
        msg = json.loads(ws.receive_text())
        if msg["type"] == "done":
            break


def test_h1_a_paisalo_client_id_resolves_tenant(non_test_settings, monkeypatch):
    """(a) client_id=paisalo → tenant=paisalo."""
    captured = _capture_turn_request(monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/brain") as ws:
            _run_one_turn(ws, extra_start={"client_id": "paisalo"})
    assert captured["request"].tenant_id == "paisalo"


def test_h1_b_legacy_no_client_id_falls_back(non_test_settings, monkeypatch):
    """(b) no client_id + agent_id=salary-on-time → salary_on_time via agent_routing."""
    captured = _capture_turn_request(monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/brain") as ws:
            _run_one_turn(ws, extra_start={"agent_id": "salary-on-time"})
    assert captured["request"].tenant_id == "salary_on_time"


def test_h1_c_test_mode_does_not_pin_tenant(monkeypatch):
    """(c) TEST_MODE + TEST_TENANT_ID=paisalo must NOT override client_id=salary-on-time."""
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("TEST_TENANT_ID", "paisalo")
    monkeypatch.setenv("TEST_FORCE_TENANT", "")
    monkeypatch.setenv("DEFAULT_TENANT_ID", "default")
    get_settings.cache_clear()
    SESSION_REGISTRY.reset()
    captured = _capture_turn_request(monkeypatch)
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/ws/brain") as ws:
                _run_one_turn(ws, extra_start={"client_id": "salary-on-time"})
        assert captured["request"].tenant_id == "salary_on_time"
    finally:
        get_settings.cache_clear()
        SESSION_REGISTRY.reset()


def test_h1_test_force_tenant_explicit_override(monkeypatch):
    """TEST_FORCE_TENANT is the only allowed pin."""
    monkeypatch.setenv("TEST_MODE", "false")
    monkeypatch.setenv("TEST_FORCE_TENANT", "paisalo")
    monkeypatch.setenv("DEFAULT_TENANT_ID", "default")
    get_settings.cache_clear()
    SESSION_REGISTRY.reset()
    captured = _capture_turn_request(monkeypatch)
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/ws/brain") as ws:
                _run_one_turn(ws, extra_start={"client_id": "salary-on-time"})
        assert captured["request"].tenant_id == "paisalo"
    finally:
        get_settings.cache_clear()
        SESSION_REGISTRY.reset()
