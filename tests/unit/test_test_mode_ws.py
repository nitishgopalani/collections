"""TEST_MODE bare session_start normalization on brain WebSocket."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.websockets import WebSocketState

from app.config import get_settings
from app.ws.handler import _normalize_test_session_start


def test_normalize_test_session_start_fills_blanks(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    get_settings.cache_clear()
    settings = get_settings()
    payload, was_bare = _normalize_test_session_start({"type": "session_start"}, settings)
    assert was_bare is True
    assert payload["session_id"]
    assert payload["borrower_id"] == "sot_test_borrower"
    assert payload["agent_id"] == "salary-on-time-test"


def test_normalize_test_session_start_noop_when_complete(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    get_settings.cache_clear()
    settings = get_settings()
    payload, was_bare = _normalize_test_session_start(
        {
            "type": "session_start",
            "session_id": "sess-1",
            "borrower_id": "b-1",
            "agent_id": "agent-1",
        },
        settings,
    )
    assert was_bare is False
    assert payload["session_id"] == "sess-1"
