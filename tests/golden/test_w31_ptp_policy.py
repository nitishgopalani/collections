"""W3-1 PTP policy engine + computed slots.

  - date >30d → counter once then accept_flagged
  - partial 50% → remainder ask
  - partial 10% → full-ask
  - computed slots deterministic
  - L3/L4 confirm fragments still resolve (regression import)
"""

from __future__ import annotations

import json
import logging
from datetime import date

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.fragment_library import get_fragment, resolve_confirm_fragment
from app.engine.nlg import spoken_date_hindi
from app.engine.ptp_policy import (
    PtpPolicyConfig,
    compute_derived_slots,
    evaluate_date,
    evaluate_partial,
    extract_offered_amount,
    nearest_acceptable_date,
    policy_from_profile,
)
from app.engine.retrieval import clear_retrieval_cache
from app.engine.tenant_profile import clear_tenant_profile_cache, get_tenant_profile
from app.engine.turn import handle_turn
from app.flows.loader import reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest

CALL_DATE = "2026-08-15"
TODAY = date(2026, 8, 15)
TENANT = "paisalo"
POLICY = PtpPolicyConfig(max_ptp_days=30, min_partial_pct=25, counter_max_attempts=1)
REPAY = 4500
FAR_ISO = "2026-09-29"  # 45 days
COUNTER_ISO = "2026-09-14"  # today+30


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("SCRIPTED_CATALOG_ROUTING", "true")
    monkeypatch.setenv("CALL_WINDOW_START", "00:00")
    monkeypatch.setenv("CALL_WINDOW_END", "23:59")
    monkeypatch.setenv("TEST_PLO_SCENARIO", "postdue3")
    monkeypatch.setenv("COMMITMENT_GATE_ENFORCE", "true")
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
    def __init__(self, turns=None):
        self._responses = [json.dumps(t, ensure_ascii=False) for t in (turns or [])]
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


def _req(call_id: str, text: str) -> TurnRequest:
    return TurnRequest(
        call_id=call_id,
        borrower_id="plo_test_borrower",
        tenant_id=TENANT,
        channel="voice",
        locale="hi-IN",
        transcript=text,
        turn_meta={"force_flow": "plo_opener", "call_date": CALL_DATE},
    )


# ---------------------------------------------------------------------------
# Pure policy
# ---------------------------------------------------------------------------


def test_policy_yaml_on_paisalo_pending_client_defaults():
    profile = get_tenant_profile(TENANT)
    cfg = policy_from_profile(profile)
    assert cfg is not None
    assert cfg.max_ptp_days == 30
    assert cfg.min_partial_pct == 25
    assert cfg.counter_max_attempts == 1


def test_evaluate_date_within_policy_accepts():
    v = evaluate_date("2026-08-25", policy=POLICY, today=TODAY)
    assert v.action == "accept"
    assert v.ptp_date == "2026-08-25"
    assert v.flagged is False


def test_evaluate_date_beyond_30d_counters_then_flags():
    v1 = evaluate_date(FAR_ISO, policy=POLICY, today=TODAY, counter_attempts=0)
    assert v1.action == "counter"
    assert v1.counter_date == COUNTER_ISO
    assert nearest_acceptable_date(TODAY, 30).isoformat() == COUNTER_ISO

    v2 = evaluate_date(FAR_ISO, policy=POLICY, today=TODAY, counter_attempts=1)
    assert v2.action == "accept_flagged"
    assert v2.flagged is True
    assert v2.ptp_date == FAR_ISO


def test_partial_50_remainder_10_full_ask():
    half = evaluate_partial(2250, REPAY, policy=POLICY)
    assert half.action == "ask_remainder"
    assert half.remaining_after == 2250

    tenth = evaluate_partial(450, REPAY, policy=POLICY)
    assert tenth.action == "ask_full"
    assert tenth.remaining_after == 4050


def test_extract_offered_amount_digits_and_hindi_words():
    assert extract_offered_amount("aadha aaj dunga", REPAY) == 2250
    assert extract_offered_amount("आधा दे दूँगा", REPAY) == 2250
    assert extract_offered_amount("main 450 rupaye dunga", REPAY) == 450
    assert extract_offered_amount("do hazaar dunga", REPAY) == 2000
    assert extract_offered_amount("10 दिन बाद दूंगा", REPAY) is None
    assert extract_offered_amount("theek hai kar dunga", REPAY) is None


def test_computed_slots_days_and_remaining():
    slots = {
        "repay_amount": 4500,
        "offered_amount": 2250,
        "due_date": "2026-08-10",
        "call_date": CALL_DATE,
    }
    derived = compute_derived_slots(slots, TODAY)
    assert derived["remaining_after"] == 2250
    assert derived["days_since_due"] == 5
    assert derived["days_to_due"] == 0

    future = compute_derived_slots(
        {"repay_amount": 4500, "due_date": "2026-08-20", "call_date": CALL_DATE},
        TODAY,
    )
    assert future["days_to_due"] == 5
    assert future["days_since_due"] == 0
    assert future["remaining_after"] == 4500


def test_fragments_and_l4_confirm_regression():
    assert get_fragment(TENANT, "ptp_counter_date")
    assert get_fragment(TENANT, "ptp_ack_remainder")
    assert get_fragment(TENANT, "ptp_full_ask")
    assert "{counter_date}" in get_fragment(TENANT, "ptp_counter_date")["text"]
    assert resolve_confirm_fragment(
        TENANT, "plo_timeline", "willing", committed_date="2026-08-25"
    ) == "confirm_pay_date"
    assert resolve_confirm_fragment(TENANT, "plo_timeline", "refused") == (
        "confirm_plo_timeline_refused"
    )


# ---------------------------------------------------------------------------
# Live-path handle_turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_beyond_30d_counter_once_then_flag(caplog):
    memory = InMemoryMemoryStore()
    call_id = "w31-beyond-30"
    llm = _ScriptedLLM()
    with caplog.at_level(logging.INFO, logger="app.engine.turn_decision_log"):
        await handle_turn(
            _req(call_id, ""), memory=memory, llm=llm,
            tools=FakeToolClient(), kb=_EmptyKB(),
        )
        await handle_turn(
            _req(call_id, "हाँ, मैं रमेश बोल रहा हूँ।"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
        t_date = await handle_turn(
            _req(call_id, "मैं 45 दिन बाद दूंगा।"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
        t_yes = await handle_turn(
            _req(call_id, "हाँ पक्का।"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
        t_no = await handle_turn(
            _req(call_id, "नहीं, 45 दिन बाद ही।"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )

    spoken_far = spoken_date_hindi(FAR_ISO)
    spoken_counter = spoken_date_hindi(COUNTER_ISO)
    assert spoken_far in (t_date.reply_text or "")
    assert "सही" in (t_date.reply_text or "")
    assert spoken_counter in (t_yes.reply_text or "")
    assert "आगे नहीं" in (t_yes.reply_text or "")

    state = await memory.load_state(call_id)
    assert state is not None
    assert state.slots.get("ptp_date") == FAR_ISO
    assert state.slots.get("ptp_beyond_policy") is True
    assert state.slots.get("disposition") == "PTP_SET"
    assert t_no.disposition == "PTP_SET"
    assert spoken_far in (t_no.reply_text or "")


@pytest.mark.asyncio
async def test_partial_50_asks_remainder():
    memory = InMemoryMemoryStore()
    call_id = "w31-partial-50"
    llm = _ScriptedLLM()
    await handle_turn(
        _req(call_id, ""), memory=memory, llm=llm,
        tools=FakeToolClient(), kb=_EmptyKB(),
    )
    await handle_turn(
        _req(call_id, "हाँ, मैं रमेश बोल रहा हूँ।"),
        memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
    )
    t = await handle_turn(
        _req(call_id, "आधा आज दूंगा।"),
        memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
    )
    assert "2250" in (t.reply_text or "")
    assert "बाकी" in (t.reply_text or "")
    state = await memory.load_state(call_id)
    assert state.slots.get("offered_amount") == 2250
    assert state.slots.get("remaining_after") == 2250
    assert state.slots.get("disposition") != "PTP_SET"


@pytest.mark.asyncio
async def test_partial_10_full_ask():
    memory = InMemoryMemoryStore()
    call_id = "w31-partial-10"
    llm = _ScriptedLLM()
    await handle_turn(
        _req(call_id, ""), memory=memory, llm=llm,
        tools=FakeToolClient(), kb=_EmptyKB(),
    )
    await handle_turn(
        _req(call_id, "हाँ, मैं रमेश बोल रहा हूँ।"),
        memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
    )
    t = await handle_turn(
        _req(call_id, "मैं 450 रुपये दूंगा।"),
        memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
    )
    assert "पूरी" in (t.reply_text or "") or "देय" in (t.reply_text or "")
    state = await memory.load_state(call_id)
    assert state.slots.get("offered_amount") == 450
    assert state.slots.get("disposition") != "PTP_SET"
