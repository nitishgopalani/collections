"""W1-B.4 — reply_empty=true logging with turn_id (silence always visible).

H2 dead-air defense: a mute turn (gate produces empty reply_text) must be
visible in the logs as a structured `reply_empty=<bool> turn_id=<uuid>`
line, greppable in isolation — not buried inside the audit record. The
gate can legitimately produce an empty reply (pure side-effect turn), but
it must never be silent.
"""
from __future__ import annotations

import json
import logging
import re

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine import turn as turn_module
from app.engine.tenant_profile import clear_tenant_profile_cache
from app.engine.turn import handle_turn
from app.flows.loader import reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest

CALL_DATE = "2026-08-06"
_UUID_RE = re.compile(r"turn_id=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("SCRIPTED_CATALOG_ROUTING", "true")
    monkeypatch.setenv("CALL_WINDOW_START", "00:00")
    monkeypatch.setenv("CALL_WINDOW_END", "23:59")
    clear_tenant_profile_cache()
    get_settings.cache_clear()
    reload_flow_set()
    yield
    clear_tenant_profile_cache()
    get_settings.cache_clear()


class _EmptyKB:
    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def retrieve(self, text, tenant_id, k: int = 6):
        return []


class _ScriptedLLM:
    def __init__(self, turns):
        self._responses = [json.dumps(t) for t in turns]
        self.call_count = 0

    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def complete(self, system, user, *, json_only=True, **kw) -> str:
        self.call_count += 1
        if self.call_count <= len(self._responses):
            return self._responses[self.call_count - 1]
        return "[]"


async def _turn(memory, call_id, text, llm):
    req = TurnRequest(
        call_id=call_id,
        borrower_id="plo_test_borrower",
        tenant_id="paisalo",
        channel="voice",
        locale="hi-IN",
        transcript=text,
        turn_meta={"force_flow": "plo_opener", "call_date": CALL_DATE},
    )
    return await handle_turn(req, memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB())


def _reply_empty_records(caplog):
    return [r for r in caplog.records if r.getMessage().startswith("reply_empty=")]


@pytest.mark.asyncio
async def test_w1b4_reply_empty_false_logged_on_normal_turn(caplog):
    """A turn that produces a non-empty reply logs `reply_empty=False turn_id=<uuid>`."""
    caplog.set_level(logging.INFO, logger="app.engine.turn")
    memory = InMemoryMemoryStore()
    llm = _ScriptedLLM([[]])  # t1 greeting → non-empty opener reply

    resp = await _turn(memory, "w1b4-normal", "", llm)

    records = _reply_empty_records(caplog)
    assert records, "reply_empty log line not emitted"
    msg = records[-1].getMessage()
    assert "reply_empty=False" in msg, msg
    m = _UUID_RE.search(msg)
    assert m, f"turn_id missing or not a uuid: {msg}"
    assert m.group(1), f"turn_id empty: {msg}"
    # The logged turn_id must match the response audit_id (silence is traceable).
    assert resp.audit_id == m.group(1), (
        f"logged turn_id {m.group(1)} != resp.audit_id {resp.audit_id}"
    )


@pytest.mark.asyncio
async def test_w1b4_reply_empty_true_logged_on_empty_reply(monkeypatch, caplog):
    """A turn whose gate produces an empty reply logs `reply_empty=True turn_id=<uuid>`.

    Monkeypatches process_outbound_reply to return empty reply_text (a pure
    side-effect turn). The reply_empty log must still fire with a valid turn_id.
    """
    caplog.set_level(logging.INFO, logger="app.engine.turn")
    memory = InMemoryMemoryStore()
    llm = _ScriptedLLM([[]])

    real = turn_module.process_outbound_reply

    def _empty_gate(draft_reply, state, request, **kw):
        text, st, transfer, chain = real(draft_reply, state, request, **kw)
        return "", st, transfer, chain

    monkeypatch.setattr(turn_module, "process_outbound_reply", _empty_gate)

    resp = await _turn(memory, "w1b4-empty", "", llm)

    records = _reply_empty_records(caplog)
    assert records, "reply_empty log line not emitted on empty reply"
    msg = records[-1].getMessage()
    assert "reply_empty=True" in msg, msg
    m = _UUID_RE.search(msg)
    assert m, f"turn_id missing or not a uuid: {msg}"
    assert m.group(1), f"turn_id empty: {msg}"
    assert resp.audit_id == m.group(1), (
        f"logged turn_id {m.group(1)} != resp.audit_id {resp.audit_id}"
    )
    # And the response reply_text is indeed empty (sanity).
    assert (resp.reply_text or "") == ""


@pytest.mark.asyncio
async def test_w1b4_reply_empty_log_carries_call_and_tenant(caplog):
    """The reply_empty log line carries call_id + tenant_id for triage."""
    caplog.set_level(logging.INFO, logger="app.engine.turn")
    memory = InMemoryMemoryStore()
    llm = _ScriptedLLM([[]])

    await _turn(memory, "w1b4-triage", "", llm)

    records = _reply_empty_records(caplog)
    assert records
    msg = records[-1].getMessage()
    assert "call_id=w1b4-triage" in msg, msg
    assert "tenant_id=paisalo" in msg, msg
