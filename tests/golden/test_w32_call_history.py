"""W3-2 call-history + mid-call memory.

  - 2nd seeded same-day call → R2 repeat greeting, never the detail dump
  - PTP honour contradiction: last_ptp_date=+5d, campaign dials today
  - mid-call payment claim → fact_payment_lag + payment_claimed
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.call_history import (
    hydrate_call_history,
    is_repeat_call,
    should_honour_ptp,
)
from app.engine.fragment_library import get_fragment
from app.engine.nlg import spoken_date_hindi
from app.engine.retrieval import clear_retrieval_cache
from app.engine.tenant_profile import clear_tenant_profile_cache, get_tenant_profile
from app.engine.tracker import new_conversation_state
from app.engine.turn import handle_turn
from app.flows.loader import reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest

CALL_DATE = "2026-08-15"
TODAY = date(2026, 8, 15)
PTP_ISO = (TODAY + timedelta(days=5)).isoformat()  # 2026-08-20
TENANT = "paisalo"
BORROWER = "plo_test_borrower"
R2_MARKER = "आज पहले भी आपसे बात हुई थी"
REMINDER_MARKER = "तक का समय लिया था"
DETAIL_MARKERS = ("किश्त", "रुपये", "बकाया")
COLLECT_ASK = "क्या आप यह भुगतान कर पाएंगे"


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
        borrower_id=BORROWER,
        tenant_id=TENANT,
        channel="voice",
        locale="hi-IN",
        transcript=text,
        turn_meta={"force_flow": "plo_opener", "call_date": CALL_DATE},
    )


async def _seed_prior(memory: InMemoryMemoryStore, **fields) -> None:
    record = {
        "call_id": "prior-w32",
        "ts": "2026-08-15T08:00:00+05:30",
        "disposition": "NO_PTP",
    }
    record.update(fields)
    await memory.upsert_session_record(BORROWER, record)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_profile_and_fragments_on():
    profile = get_tenant_profile(TENANT)
    assert profile is not None
    assert profile.supports_call_history is True
    assert profile.cues("payment_claim")
    greet = get_fragment(TENANT, "repeat_call_greeting")
    assert greet is not None
    assert R2_MARKER in greet["text"]
    rem = get_fragment(TENANT, "ptp_reminder")
    assert rem is not None
    assert "{ptp_date}" in rem["text"]
    assert REMINDER_MARKER in rem["text"]


def test_hydrate_from_session_records():
    state = new_conversation_state("new-call", TENANT, BORROWER)
    prior = [
        {
            "call_id": "old-1",
            "ts": "2026-08-15T08:00:00+05:30",
            "disposition": "PTP_SET",
            "ptp_date": PTP_ISO,
        }
    ]
    hydrated = hydrate_call_history(state, prior, TODAY)
    assert hydrated.slots["attempts_today"] == 2
    assert hydrated.slots["last_disposition"] == "PTP_SET"
    assert hydrated.slots["last_ptp_date"] == PTP_ISO
    assert hydrated.slots["repeat_call"] is True
    assert hydrated.slots["ptp_honour"] is True
    assert is_repeat_call("2026-08-15T08:00:00+05:30", TODAY) is True
    assert should_honour_ptp(PTP_ISO, TODAY) is True
    assert should_honour_ptp(CALL_DATE, TODAY) is False


# ---------------------------------------------------------------------------
# Live-path handle_turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeat_call_greeting_skips_detail_dump():
    memory = InMemoryMemoryStore()
    await _seed_prior(memory)
    t = await handle_turn(
        _req("w32-repeat", ""),
        memory=memory,
        llm=_ScriptedLLM(),
        tools=FakeToolClient(),
        kb=_EmptyKB(),
    )
    spoken = t.reply_text or ""
    assert R2_MARKER in spoken
    assert "रमेश" in spoken
    assert COLLECT_ASK in spoken
    for marker in DETAIL_MARKERS:
        assert marker not in spoken
    state = await memory.load_state("w32-repeat")
    assert state is not None
    assert state.slots.get("attempts_today") == 2
    assert state.slots.get("repeat_call") is True
    assert state.slots.get("identity_ok") is True
    assert state.flow_stack
    assert state.flow_stack[0].flow == "plo_postdue3"
    records = await memory.list_sessions(BORROWER)
    assert any(r.get("call_id") == "w32-repeat" for r in records)


@pytest.mark.asyncio
async def test_ptp_honour_contradiction_reminds_no_collect():
    memory = InMemoryMemoryStore()
    await _seed_prior(
        memory,
        disposition="PTP_SET",
        ptp_date=PTP_ISO,
    )
    t = await handle_turn(
        _req("w32-ptp-honour", ""),
        memory=memory,
        llm=_ScriptedLLM(),
        tools=FakeToolClient(),
        kb=_EmptyKB(),
    )
    spoken = t.reply_text or ""
    spoken_ptp = spoken_date_hindi(PTP_ISO)
    assert REMINDER_MARKER in spoken
    assert spoken_ptp in spoken
    assert COLLECT_ASK not in spoken
    for marker in DETAIL_MARKERS:
        assert marker not in spoken
    assert t.disposition == "PTP_REMINDED"
    assert t.end_call is True
    state = await memory.load_state("w32-ptp-honour")
    assert state is not None
    assert state.slots.get("disposition") == "PTP_REMINDED"
    assert state.slots.get("ptp_date") == PTP_ISO
    assert state.slots.get("ptp_honour") is True


@pytest.mark.asyncio
async def test_payment_claim_sets_flag_and_lag_fragment():
    memory = InMemoryMemoryStore()
    llm = _ScriptedLLM()
    await handle_turn(
        _req("w32-claim", ""),
        memory=memory,
        llm=llm,
        tools=FakeToolClient(),
        kb=_EmptyKB(),
    )
    await handle_turn(
        _req("w32-claim", "हाँ, मैं रमेश बोल रहा हूँ।"),
        memory=memory,
        llm=llm,
        tools=FakeToolClient(),
        kb=_EmptyKB(),
    )
    t = await handle_turn(
        _req("w32-claim", "abhi kiya QR se pay kar diya"),
        memory=memory,
        llm=llm,
        tools=FakeToolClient(),
        kb=_EmptyKB(),
    )
    spoken = t.reply_text or ""
    assert "अपडेट होने में थोड़ा समय" in spoken
    assert "कानपुर" in spoken
    state = await memory.load_state("w32-claim")
    assert state is not None
    assert state.slots.get("payment_claimed") is True
    assert state.slots.get("attempts_today") == 1
    assert state.slots.get("repeat_call") is not True
