"""HARDEN-1 F1 — opener turn exception → deterministic template greeting.

When the opener turn (empty transcript) crashes (e.g. transient DNS on
persist/audit → "[Errno -2] Name or service not known"), the caller must still
hear a greeting rendered via NLG (no LLM/KB dependency) instead of dead air.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.ws.tenant_limits import SESSION_REGISTRY


@pytest.fixture
def paisalo_settings(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "false")
    monkeypatch.setenv("TEST_FORCE_TENANT", "")
    monkeypatch.setenv("DEFAULT_TENANT_ID", "default")
    get_settings.cache_clear()
    SESSION_REGISTRY.reset()
    yield
    get_settings.cache_clear()
    SESSION_REGISTRY.reset()


def _start_session(ws, *, session_id="s1", client_id="paisalo"):
    ws.send_json(
        {
            "type": "session_start",
            "session_id": session_id,
            "borrower_id": "b-1",
            "agent_id": "agent-generic",
            "client_id": client_id,
        }
    )
    ready = json.loads(ws.receive_text())
    assert ready["type"] == "session_ready"
    assert ready["session_id"] == session_id


def _collect_until_done(ws, *, max_msgs=40):
    """Collect all WS messages until a done arrives; return (chunks, done, flow_classes)."""
    chunks: list[str] = []
    flow_classes: list[str] = []
    done = None
    for _ in range(max_msgs):
        msg = json.loads(ws.receive_text())
        if msg["type"] == "chunk":
            chunks.append(msg["text"])
        elif msg["type"] == "flow_class":
            flow_classes.append(msg.get("next", ""))
        elif msg["type"] == "done":
            done = msg
            break
        elif msg["type"] == "error":
            pytest.fail(f"unexpected error message: {msg}")
    return chunks, done, flow_classes


def test_f1_opener_dns_failure_speaks_template_greeting(paisalo_settings, monkeypatch):
    """Opener turn raises a DNS error → caller hears plo_greeting_unknown via NLG."""

    async def boom(*_args, **_kwargs):
        raise OSError("[Errno -2] Name or service not known")

    monkeypatch.setattr("app.ws.handler.handle_turn", AsyncMock(side_effect=boom))

    with TestClient(app) as client:
        with client.websocket_connect("/ws/brain") as ws:
            _start_session(ws)
            ws.send_json(
                {
                    "type": "turn",
                    "session_id": "s1",
                    "turn_id": "t1",
                    "transcript": "",
                }
            )
            chunks, done, flow_classes = _collect_until_done(ws)

    # The opener fallback must speak the plo_greeting_unknown template text.
    spoken = "".join(chunks).strip()
    assert "पैसालो" in spoken, f"expected plo_greeting_unknown greeting; got {spoken!r}"
    # Done must carry the OPENER_FALLBACK disposition and NOT end the call.
    assert done is not None
    assert done["disposition"] == "OPENER_FALLBACK"
    assert done["end_call"] is False


def test_f1_non_opener_dns_failure_sends_error_not_greeting(paisalo_settings, monkeypatch):
    """A non-opener turn (non-empty transcript) that crashes → error, not a greeting."""

    async def boom(*_args, **_kwargs):
        raise OSError("[Errno -2] Name or service not known")

    monkeypatch.setattr("app.ws.handler.handle_turn", AsyncMock(side_effect=boom))

    with TestClient(app) as client:
        with client.websocket_connect("/ws/brain") as ws:
            _start_session(ws)
            ws.send_json(
                {
                    "type": "turn",
                    "session_id": "s1",
                    "turn_id": "t1",
                    "transcript": "नमस्ते",
                }
            )
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "error", f"expected error for non-opener crash; got {msg}"
            assert msg.get("fallback_text"), "error must carry a fallback_text"


def test_f1_extract_failure_url_walks_exception_chain(monkeypatch):
    """_extract_failure_url finds the URL on an httpx-style exception chain."""
    from app.ws.handler import _extract_failure_url

    class FakeRequest:
        url = "https://fluent-tadpole-28655.upstash.io/memory"

    class FakeHttpxError(Exception):
        def __init__(self, request: FakeRequest) -> None:
            self.request = request
            super().__init__("dns failed")

    gai = OSError("[Errno -2] Name or service not known")
    httpx_err = FakeHttpxError(FakeRequest())
    gai.__cause__ = httpx_err
    url = _extract_failure_url(gai)
    assert "fluent-tadpole-28655.upstash.io" in url, f"expected upstash URL; got {url!r}"


def test_f1_opener_fallback_uses_safe_fallback_when_no_reply_id(monkeypatch, paisalo_settings):
    """A tenant with no opener_fallback_reply_id falls back to safe_fallback_reply."""
    from app.config import tenant_config

    # Simulate a tenant without opener_fallback_reply_id by clearing it.
    cfg = tenant_config("paisalo")
    monkeypatch.setattr(cfg, "opener_fallback_reply_id", "")
    monkeypatch.setattr(cfg, "safe_fallback_reply", "STATIC FALLBACK TEXT")
    # Patch tenant_config to return our mutated cfg.
    monkeypatch.setattr("app.ws.handler.tenant_config", lambda _tid: cfg)

    async def boom(*_args, **_kwargs):
        raise OSError("[Errno -2] Name or service not known")

    monkeypatch.setattr("app.ws.handler.handle_turn", AsyncMock(side_effect=boom))

    with TestClient(app) as client:
        with client.websocket_connect("/ws/brain") as ws:
            _start_session(ws)
            ws.send_json(
                {
                    "type": "turn",
                    "session_id": "s1",
                    "turn_id": "t1",
                    "transcript": "",
                }
            )
            chunks, done, _ = _collect_until_done(ws)

    spoken = "".join(chunks).strip()
    assert "STATIC FALLBACK TEXT" in spoken, f"expected safe_fallback; got {spoken!r}"
    assert done["disposition"] == "OPENER_FALLBACK"
