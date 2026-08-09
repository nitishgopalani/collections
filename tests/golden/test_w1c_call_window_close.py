"""W1-C C3 — call-window close-out (policy interrupt).

Verifies the policy-lane call-window close-out fires BEFORE the Tier-1
evidence scorer when an ANSWERED call crosses the configured window
boundary mid-conversation. Speaks the scripted polite close, tags
``disposition=call_window_closed``, and graceful ENDs (outcome 7) — never a
mid-call ``silent_reply``. Only fires mid-call (``attempts >= 1``); the first
turn (call initiation outside the window) is left to the gate's silent
``outside_call_window`` block (correct — do not answer).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings, tenant_config
from app.engine.compliance_rules import within_call_window
from app.engine.retrieval import clear_retrieval_cache
from app.engine.safety import call_window_preempt
from app.engine.tenant_profile import clear_tenant_profile_cache
from app.engine.tracker import new_conversation_state
from app.engine.turn import handle_turn
from app.flows.loader import reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest

CALL_DATE = "2026-08-06"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("SCRIPTED_CATALOG_ROUTING", "true")
    monkeypatch.setenv("CALL_WINDOW_START", "00:00")
    monkeypatch.setenv("CALL_WINDOW_END", "23:59")
    clear_tenant_profile_cache()
    get_settings.cache_clear()
    reload_flow_set()
    clear_retrieval_cache()
    yield
    clear_retrieval_cache()
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

    async def complete(self, system: str, user: str, *, json_only: bool = True, **kw) -> str:
        self.call_count += 1
        if self.call_count <= len(self._responses):
            return self._responses[self.call_count - 1]
        return "[]"


async def _turn(memory, call_id, text, llm, *, turn_meta=None):
    req = TurnRequest(
        call_id=call_id,
        borrower_id="plo_cw_borrower",
        tenant_id="paisalo",
        channel="voice",
        locale="hi-IN",
        transcript=text,
        turn_meta=turn_meta or {"force_flow": "plo_opener", "call_date": CALL_DATE},
    )
    return await handle_turn(
        req,
        memory=memory,
        llm=llm,
        tools=FakeToolClient(),
        kb=_EmptyKB(),
    )


def test_c3_call_window_preempt_skips_first_turn():
    """First turn (attempts=0) never fires call_window_preempt — even if
    outside the window. Call initiation outside the window is the gate's job
    (silent block, do not answer), NOT the policy-lane preempt."""
    from zoneinfo import ZoneInfo

    cfg = tenant_config("paisalo").model_copy(update={"call_window_start": "08:00", "call_window_end": "19:00"})
    state = new_conversation_state("c-cw-0", "paisalo", "plo_cw_borrower")
    state.attempts = 0
    late = datetime(2026, 8, 6, 23, 30, tzinfo=ZoneInfo(cfg.call_window_timezone))
    result = call_window_preempt(state, cfg, now=late)
    assert result is None, "first turn (attempts=0) must not fire call_window_preempt"


def test_c3_call_window_preempt_skips_when_inside_window():
    cfg = tenant_config("paisalo")
    state = new_conversation_state("c-cw-in", "paisalo", "plo_cw_borrower")
    state.attempts = 2
    from zoneinfo import ZoneInfo

    inside = datetime(2026, 8, 6, 12, 0, tzinfo=ZoneInfo(cfg.call_window_timezone))
    result = call_window_preempt(state, cfg, now=inside)
    assert result is None, "mid-call but inside window → no close"


def test_c3_call_window_preempt_fires_mid_call_outside_window():
    cfg = tenant_config("paisalo").model_copy(update={"call_window_start": "08:00", "call_window_end": "19:00"})
    state = new_conversation_state("c-cw-out", "paisalo", "plo_cw_borrower")
    state.attempts = 2
    from zoneinfo import ZoneInfo

    late = datetime(2026, 8, 6, 20, 5, tzinfo=ZoneInfo(cfg.call_window_timezone))
    result = call_window_preempt(state, cfg, now=late)
    assert result is not None
    assert result.end_call is True
    assert result.reason == "call_window_crossed_mid_call"
    assert result.reply_text.strip(), "polite close reply must not be empty"
    assert result.compliance_updates.get("call_window_closed") is True


@pytest.mark.asyncio
async def test_c3_mid_call_window_cross_closes_gracefully(monkeypatch):
    """Integration: turns 1-2 run inside the window; turn 3 crosses the
    boundary mid-conversation → scripted polite close + hangup, never silent.

    Monkeypatches ``call_window_preempt`` in the turn module so the first two
    turns see "inside window" (None → no close) and the third sees "crossed"
    (close result). This isolates the policy-lane wiring from the real clock
    and from the gate's own ``within_call_window`` calls.
    """
    import app.engine.turn as turn_mod

    call_count = {"n": 0}
    _close_reply = "Aapka samay dhanyavaad. Ab humein is call ko samapt karna hoga."

    def _fake_call_window_preempt(state, tenant_cfg, *, now=None, profile=None):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            return None  # turns 1-2 inside window
        from app.schemas.compliance import SafetyResult

        return SafetyResult(
            reason="call_window_crossed_mid_call",
            reply_text=tenant_cfg.call_window_close_reply or _close_reply,
            transfer_to_human=False,
            suspend_recovery=False,
            end_call=True,
            compliance_updates={"call_window_closed": True},
        )

    monkeypatch.setattr(turn_mod, "call_window_preempt", _fake_call_window_preempt)

    memory = InMemoryMemoryStore()
    llm = _ScriptedLLM(
        [
            [{"command": "start_flow", "flow": "plo_opener"}],
            [{"command": "set_slot", "name": "plo_identity", "value": "yes"}],
        ]
    )

    # Turn 1 — inside window, normal opener.
    first = await _turn(memory, "call-cw-cross", "haan", llm)
    assert first.disposition != "call_window_closed"
    assert not first.end_call

    # Turn 2 — still inside window, normal.
    second = await _turn(memory, "call-cw-cross", "haan", llm)
    assert second.disposition != "call_window_closed"
    assert not second.end_call

    # Turn 3 — window crossed mid-conversation → scripted polite close + END.
    third = await _turn(memory, "call-cw-cross", "haan", llm)
    assert third.disposition == "call_window_closed", (
        f"turn 3 disposition={third.disposition!r}, want call_window_closed"
    )
    assert third.end_call is True
    assert third.reply_text.strip(), "polite close reply must not be empty (never silent)"
    # Scorer did not run on the closing turn (policy-lane preempt).
    assert llm.call_count == 2, "scorer must not run on the call-window close turn"
