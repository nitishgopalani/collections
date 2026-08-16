"""PLO-OOF P4 — Audit B-side quick wins.

  G-B6-01: OOC golden fixture uses transcript= (not text=).
  G-B6-02: executor returns LAST utter as reply_id (not FIRST).
  G-B3-01: plo_obj_npa_third_party captures callback_window before END.
  G-B4-01: days_past_due_words helper (Hindi words, no "rupaye").
  G-B4-03: new-loan phone spoken digit-by-digit in Hindi words.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.identity_gate import slots_for_nlg
from app.engine.nlg import spoken_days_hindi
from app.engine.retrieval import clear_retrieval_cache
from app.engine.tenant_profile import clear_tenant_profile_cache
from app.engine.turn import handle_turn
from app.flows.loader import get_flow_set, reload_flow_set
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


# ---------------------------------------------------------------------------
# G-B6-01: OOC golden fixture uses transcript=
# ---------------------------------------------------------------------------


def test_p4_gb6_01_ooc_fixture_uses_transcript_field():
    """The paisalo golden _turn helper must use transcript= (not text=)."""
    import inspect

    from tests.golden import test_paisalo_scenarios as mod

    src = inspect.getsource(mod._turn)
    assert "transcript=text" in src, "OOC golden _turn must use transcript=text"
    assert "text=text" not in src, "OOC golden _turn must NOT use text=text"


# ---------------------------------------------------------------------------
# G-B6-02: executor returns LAST utter as reply_id
# ---------------------------------------------------------------------------


def test_p4_gb6_02_executor_returns_last_utter_as_reply_id():
    """Executor reply_id is the LAST utter in a chained walk (G-B6-02)."""
    import inspect

    from app.engine import executor as mod

    src = inspect.getsource(mod)
    # The "keep first" guard (`if reply_id is None: reply_id = step.utter`)
    # must be gone; the last-utter assignment must be present.
    assert "if reply_id is None:" not in src, (
        "executor still keeps FIRST utter as reply_id (G-B6-02 regression)"
    )
    assert "reply_id = step.utter" in src


# ---------------------------------------------------------------------------
# G-B3-01: plo_obj_npa_third_party captures callback_window before END
# ---------------------------------------------------------------------------


def test_p4_gb3_01_npa_third_party_captures_callback_before_end():
    """plo_obj_npa_third_party must collect callback_window before hangup."""
    flows = get_flow_set()
    flow = flows.flows.get("plo_obj_npa_third_party")
    assert flow is not None, "plo_obj_npa_third_party flow missing"
    steps = flow.steps
    # There must be a `collect: callback_window` step BEFORE any hangup/end.
    collect_idx = next(
        (i for i, s in enumerate(steps) if s.collect == "callback_window"), None
    )
    hang_idx = next(
        (i for i, s in enumerate(steps) if s.action == "hangup_call"), None
    )
    assert collect_idx is not None, "callback_window collect step missing"
    assert hang_idx is not None, "hangup step missing"
    assert collect_idx < hang_idx, "callback capture must come BEFORE hangup"


# ---------------------------------------------------------------------------
# G-B4-01: days_past_due_words helper (Hindi words, no "rupaye")
# ---------------------------------------------------------------------------


def test_p4_gb4_01_spoken_days_hindi_helper():
    assert spoken_days_hindi(5) == "पाँच"
    assert spoken_days_hindi(15) == "पंद्रह"
    assert spoken_days_hindi(30) == "तीस"
    # Negative DPD (predue) spoken as absolute value.
    assert spoken_days_hindi(-5) == "पाँच"


def test_p4_gb4_01_slots_for_nlg_derives_days_past_due_words():
    slots = {"days_past_due": 15, "identity_ok": True}
    out = slots_for_nlg(slots)
    assert out["days_past_due_words"] == "पंद्रह"


def test_p4_gb4_01_which_emi_uses_words_not_numeric():
    """The which-EMI reply must reference {days_past_due_words}, not {days_past_due}."""
    flows = get_flow_set()
    variants = flows.responses.get("plo_obj_which_emi", [])
    assert variants, "plo_obj_which_emi reply missing"
    for v in variants:
        assert "{days_past_due_words}" in v.text, (
            f"which-EMI reply must use days_past_due_words: {v.text!r}"
        )
        assert "{days_past_due}" not in v.text, (
            f"which-EMI reply still uses raw {days_past_due}: {v.text!r}"
        )


# ---------------------------------------------------------------------------
# G-B4-03: new-loan phone spoken digit-by-digit in Hindi words
# ---------------------------------------------------------------------------


def test_p4_gb4_03_new_loan_phone_digit_by_digit_hindi():
    """New-loan replies must NOT contain the literal +918035317323; instead the
    Hindi digit-by-digit words."""
    flows = get_flow_set()
    for rid in ("plo_obj_npa_new_loan", "plo_obj_new_loan_pd"):
        variants = flows.responses.get(rid, [])
        assert variants, f"{rid} reply missing"
        for v in variants:
            assert "+918035317323" not in v.text, (
                f"{rid} still has literal phone: {v.text!r}"
            )
            # Digit-by-digit Hindi words for 918035317323.
            assert "नौ एक आठ शून्य तीन पाँच तीन एक सात तीन दो तीन" in v.text, (
                f"{rid} missing digit-by-digit Hindi words: {v.text!r}"
            )


# ---------------------------------------------------------------------------
# Integration: NPA third-party path captures callback before END
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
async def test_p4_gb3_01_npa_third_party_integration_records_callback(monkeypatch, caplog):
    """NPA third-party objection: third party gives a callback time → recorded."""
    monkeypatch.setenv("TEST_PLO_SCENARIO", "npa")
    get_settings.cache_clear()
    caplog.set_level(logging.INFO, logger="app.engine.turn_decision_log")
    memory = InMemoryMemoryStore()
    call_id = "plo-oof-p4-third-party"
    # Drive into NPA, then trigger the third-party objection.
    llm = _ScriptedLLM(
        [
            [{"command": "set_slot", "name": "plo_consent_2min", "value": "yes"}],
            # t4: third party answers → coerce to plo_obj_npa_third_party.
            [{"command": "start_flow", "flow": "plo_obj_npa_third_party"}],
            # t5: third party gives a callback window.
            [{"command": "set_slot", "name": "callback_window", "value": "shaam 5 baje"}],
        ]
    )

    await _turn(memory, call_id, "", llm)  # t1 greeting
    await _turn(memory, call_id, "haan ramesh bol raha hoon", llm)  # t2 identity
    await _turn(memory, call_id, "haan do minute baat ho sakti hai", llm)  # t3 consent
    r4 = await _turn(memory, call_id, "woh yahan nahi hain", llm)  # t4 third party
    # The third-party objection reply must ask for a callback time.
    assert "किस समय वापस कॉल" in (r4.reply_text or "") or "उपलब्ध नहीं हैं" in (
        r4.reply_text or ""
    )
    # t5: third party gives a callback window → recorded + ack + hangup.
    r5 = await _turn(memory, call_id, "shaam 5 baje call karo", llm)
    state = await memory.load_state(call_id)
    assert state.slots.get("callback_window") == "shaam 5 baje", (
        f"callback_window not captured: {state.slots.get('callback_window')!r}"
    )
    # The ack reply must be spoken (plo_npa_callback_ack).
    assert "किस समय वापस कॉल" in (r5.reply_text or "") or "ठीक है" in (
        r5.reply_text or ""
    )
