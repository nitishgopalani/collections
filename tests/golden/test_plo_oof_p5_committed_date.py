"""PLO-OOF P5 — G-B4-02 committed_date hydration + assurance-date writeback.

  - committed_date added to hydration keys (tracker / postgres / test_borrower /
    command_gen context) so it can be hydrated from DB (prior commitment) and
    set during the call.
  - coerce_committed_date writes committed_date (ISO) when the borrower names a
    date in the timeline, and routes plo_timeline → specific_date so the
    assurance-date reply (plo_npa_assurance_date) speaks the committed date.
"""

from __future__ import annotations

import json
import logging
from datetime import date

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.retrieval import clear_retrieval_cache
from app.engine.scripted_coercions import (
    _extract_committed_date,
    coerce_committed_date,
)
from app.engine.tenant_profile import clear_tenant_profile_cache, load_tenant_profile
from app.engine.tracker import _HYDRATION_LOAN_KEYS
from app.engine.turn import handle_turn
from app.flows.loader import reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.schemas.command import Command

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


# ---------------------------------------------------------------------------
# G-B4-02 part 1: committed_date is in hydration keys
# ---------------------------------------------------------------------------


def test_p5_committed_date_in_tracker_hydration_keys():
    assert "committed_date" in _HYDRATION_LOAN_KEYS


def test_p5_committed_date_in_postgres_borrowers_mapping():
    import inspect

    from app.memory import postgres_borrowers as mod

    src = inspect.getsource(mod.row_to_borrower)
    assert '"committed_date"' in src, "postgres_borrowers must map committed_date"


def test_p5_committed_date_in_test_borrower_keys():
    from app.memory.test_borrower import _PLO_LOAN_KEYS

    assert "committed_date" in _PLO_LOAN_KEYS


def test_p5_committed_date_in_command_gen_context_slots():
    from app.engine.command_gen import FACT_SLOTS_FOR_RESPOND

    assert "committed_date" in FACT_SLOTS_FOR_RESPOND


# ---------------------------------------------------------------------------
# G-B4-02 part 2: _extract_committed_date + coerce_committed_date
# ---------------------------------------------------------------------------


def test_p5_extract_iso_date():
    assert _extract_committed_date("15 August ko dunga") == "2026-08-15"
    assert _extract_committed_date("2026-08-15") == "2026-08-15"
    assert _extract_committed_date("15/08/2026") == "2026-08-15"
    assert _extract_committed_date("15-08-2026") == "2026-08-15"
    assert _extract_committed_date("15 अगस्त को") == "2026-08-15"
    assert _extract_committed_date("aug 15") == "2026-08-15"
    assert _extract_committed_date("kuch nahi") is None


def test_p5_coerce_committed_date_sets_slot_and_routes_specific():
    profile = load_tenant_profile("paisalo")
    commands: list[Command] = []
    out, fired = coerce_committed_date(
        commands, "plo_timeline", "15 August ko dunga", profile=profile
    )
    assert fired
    slot_cmds = {c.name: c.value for c in out if c.command == "set_slot"}
    assert slot_cmds["committed_date"] == "2026-08-15"
    assert slot_cmds["plo_timeline"] == "specific_date"


def test_p5_coerce_committed_date_no_date_does_not_fire():
    profile = load_tenant_profile("paisalo")
    commands: list[Command] = []
    out, fired = coerce_committed_date(
        commands, "plo_timeline", "nahi de sakta", profile=profile
    )
    assert not fired
    assert out == commands


def test_p5_coerce_committed_date_wrong_slot_does_not_fire():
    profile = load_tenant_profile("paisalo")
    commands: list[Command] = []
    out, fired = coerce_committed_date(
        commands, "plo_payment_intent", "15 August ko dunga", profile=profile
    )
    assert not fired


# ---------------------------------------------------------------------------
# Integration: NPA assurance-date flow speaks the committed date
# ---------------------------------------------------------------------------


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


async def _turn(memory, call_id, text, llm, *, turn_meta=None):
    req = TurnRequest(
        call_id=call_id,
        borrower_id="plo_test_borrower",
        tenant_id="paisalo",
        channel="voice",
        locale="hi-IN",
        transcript=text,
        turn_meta=turn_meta or {"force_flow": "plo_opener", "call_date": CALL_DATE},
    )
    return await handle_turn(req, memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB())


@pytest.mark.asyncio
async def test_p5_npa_assurance_date_speaks_committed_date(monkeypatch, caplog):
    """NPA: borrower gives "15 August" → committed_date written + assurance-date reply."""
    monkeypatch.setenv("TEST_PLO_SCENARIO", "npa")
    get_settings.cache_clear()
    caplog.set_level(logging.INFO, logger="app.engine.turn_decision_log")
    memory = InMemoryMemoryStore()
    call_id = "plo-oof-p5-committed-date"
    llm = _ScriptedLLM(
        [
            [{"command": "set_slot", "name": "plo_consent_2min", "value": "yes"}],
            # t4: borrower commits to a date — coercion sets committed_date + plo_timeline=specific_date.
            [],
        ]
    )

    await _turn(memory, call_id, "", llm)  # t1 greeting
    await _turn(memory, call_id, "haan ramesh bol raha hoon", llm)  # t2 identity
    await _turn(memory, call_id, "haan do minute baat ho sakti hai", llm)  # t3 consent → disclosure → wait_timeline
    r4 = await _turn(memory, call_id, "15 August ko dunga", llm)  # t4 timeline

    state = await memory.load_state(call_id)
    assert state.slots.get("committed_date") == "2026-08-15", (
        f"committed_date not written: {state.slots.get('committed_date')!r}"
    )
    assert state.slots.get("plo_timeline") == "specific_date"
    # The assurance-date reply must be spoken (plo_npa_assurance_date).
    assert r4.reply_id == "plo_npa_assurance_date", r4.reply_id
    # The spoken text must contain the committed date (15 August → spoken form).
    assert "August" in (r4.reply_text or "") or "अगस्त" in (r4.reply_text or ""), (
        f"assurance-date reply missing the date: {r4.reply_text!r}"
    )
