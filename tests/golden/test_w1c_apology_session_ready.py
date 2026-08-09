"""W1-C C0 (DEBT-026): brain session_ready carries the tenant apology line.

Verifies the brain→go-server plumbing for invariant #10: a session_start
with a tenant that owns a profile (paisalo) yields a session_ready ack
carrying the tenant's ``apology_dead_air`` text + ``voice_id`` so the
go-server's DeadAirHandler can speak it via TTS before clean-close on
ASR-reconnect-exhaustion. Open tenants (no profile) leave both empty.
"""

import json

import pytest
from starlette.testclient import TestClient

from app.config import get_settings
from app.main import app


@pytest.fixture(autouse=True)
def _widen_call_window(monkeypatch):
    monkeypatch.setenv("CALL_WINDOW_START", "00:00")
    monkeypatch.setenv("CALL_WINDOW_END", "23:59")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _session_start(client_id: str) -> dict:
    return {
        "type": "session_start",
        "session_id": "sess-c0-1",
        "borrower_id": "bor-c0",
        "agent_id": "agent-c0",
        "pack_id": "pack-c0",
        "locale": "hi-IN",
        "client_id": client_id,
    }


def test_c0_paisalo_session_ready_carries_apology_line():
    with TestClient(app) as client:
        with client.websocket_connect("/ws/brain") as ws:
            ws.send_json(_session_start("paisalo"))
            ready = json.loads(ws.receive_text())
            assert ready["type"] == "session_ready"
            assert ready["session_id"] == "sess-c0-1"
            # apology_dead_air is populated in paisalo.yml (PENDING-CLIENT-APPROVAL
            # candidate #55). Voice may be empty if the profile doesn't set
            # voice_id; the text is the contract.
            assert ready.get("apology_text", "").strip(), (
                "paisalo session_ready must carry apology_text; got: "
                f"{ready.get('apology_text')!r}"
            )
            assert "तकनीकी समस्या" in ready["apology_text"], ready["apology_text"]


def test_c0_open_tenant_session_ready_apology_empty_when_no_profile():
    # A tenant with no registered profile → apology_text empty (handler closes
    # silently). Use a fabricated client_id that resolves to a tenant with no
    # profile YAML.
    with TestClient(app) as client:
        with client.websocket_connect("/ws/brain") as ws:
            ws.send_json(_session_start("nonexistent-tenant-c0"))
            ready = json.loads(ws.receive_text())
            assert ready["type"] == "session_ready"
            assert ready.get("apology_text", "") == "", (
                "open tenant session_ready apology_text must be empty; got: "
                f"{ready.get('apology_text')!r}"
            )
