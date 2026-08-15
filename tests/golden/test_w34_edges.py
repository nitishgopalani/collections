"""W3-4 edges + debt.

  - inbound DID: greeting + callback → INBOUND_RETURN
  - LLM 429/timeout → llm_degraded, call survives
  - multi-loan highest-DPD + multi_loan flag
  - persist-async: Upstash off critical path; InMemory stays sync
  - DEBT-038 slot-segmented TTS keys
  - DEBT-043 consent enum forms
"""

from __future__ import annotations

import json

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.command_gen import is_llm_degrade_error
from app.engine.fragment_library import get_fragment
from app.engine.multi_loan import select_winning_loan
from app.engine.retrieval import clear_retrieval_cache
from app.engine.scripted_coercions import coerce_consent
from app.engine.tenant_profile import clear_tenant_profile_cache, get_tenant_profile
from app.engine.tracker import hydrate_from_borrower, new_conversation_state
from app.engine.tts_segments import segment_spoken_reply, split_template_static
from app.engine.turn import _persist_off_critical_path, handle_turn
from app.flows.loader import reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.schemas.command import Command
from app.schemas.state import BorrowerRecord

CALL_DATE = "2026-08-15"
TENANT = "paisalo"
BORROWER = "plo_test_borrower"
DETAIL_MARKERS = ("किश्त", "₹", "4500")


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


class _DegradeLLM:
    def __init__(self, exc: BaseException):
        self.exc = exc
        self.call_count = 0

    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def complete(self, system: str, user: str, *, json_only: bool = True, **kw) -> str:
        self.call_count += 1
        raise self.exc


def _req(call_id: str, text: str, *, direction: str | None = None) -> TurnRequest:
    meta: dict = {"call_date": CALL_DATE}
    if direction:
        meta["direction"] = direction
    return TurnRequest(
        call_id=call_id,
        borrower_id=BORROWER,
        tenant_id=TENANT,
        channel="voice",
        locale="hi-IN",
        transcript=text,
        turn_meta=meta,
    )


# ---------------------------------------------------------------------------
# Profile / fragments
# ---------------------------------------------------------------------------


def test_inbound_profile_and_greeting_fragment():
    profile = get_tenant_profile(TENANT)
    assert profile is not None
    assert profile.supports_inbound_did is True
    assert profile.helpline
    greet = get_fragment(TENANT, "inbound_greeting")
    assert greet is not None
    assert "{branch}" in greet["text"]
    assert "{helpline}" in greet["text"]
    for marker in ("रमेश", "4500", "{customer_name}", "{repay_amount}"):
        assert marker not in greet["text"]


# ---------------------------------------------------------------------------
# Inbound DID
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_did_greeting_then_callback_return():
    memory = InMemoryMemoryStore()
    llm = _ScriptedLLM()
    t1 = await handle_turn(
        _req("w34-in", "", direction="inbound"),
        memory=memory,
        llm=llm,
        tools=FakeToolClient(),
        kb=_EmptyKB(),
    )
    spoken = t1.reply_text or ""
    assert t1.disposition == "INBOUND_RETURN"
    assert t1.end_call is False
    assert "कानपुर" in spoken
    assert "हेल्पलाइन" in spoken or "पैसालो" in spoken
    assert "वापस कॉल" in spoken
    for marker in DETAIL_MARKERS:
        assert marker not in spoken
    assert "रमेश" not in spoken
    assert llm.call_count == 0
    state = await memory.load_state("w34-in")
    assert state is not None
    assert state.slots.get("disposition") == "INBOUND_RETURN"
    assert state.slots.get("_inbound_open") is True

    t2 = await handle_turn(
        _req("w34-in", "kal subah das baje", direction="inbound"),
        memory=memory,
        llm=llm,
        tools=FakeToolClient(),
        kb=_EmptyKB(),
    )
    assert t2.disposition == "INBOUND_RETURN"
    assert t2.end_call is True
    assert "वापस कॉल" in (t2.reply_text or "")
    assert llm.call_count == 0
    state = await memory.load_state("w34-in")
    assert state is not None
    assert state.slots.get("callback_window") == "kal subah das baje"
    assert state.slots.get("disposition") == "INBOUND_RETURN"


# ---------------------------------------------------------------------------
# LLM degrade
# ---------------------------------------------------------------------------


def test_is_llm_degrade_error():
    assert is_llm_degrade_error(TimeoutError("timed out"))
    assert is_llm_degrade_error(Exception("429 Too Many Requests"))
    assert is_llm_degrade_error(Exception("Resource exhausted"))
    assert not is_llm_degrade_error(ValueError("bad json"))


@pytest.mark.asyncio
async def test_llm_429_degrades_and_call_survives():
    memory = InMemoryMemoryStore()
    llm = _DegradeLLM(Exception("429 Too Many Requests"))
    await handle_turn(
        _req("w34-429", ""),
        memory=memory,
        llm=llm,
        tools=FakeToolClient(),
        kb=_EmptyKB(),
    )
    t = await handle_turn(
        _req("w34-429", "kal mausam kaisa hoga"),
        memory=memory,
        llm=llm,
        tools=FakeToolClient(),
        kb=_EmptyKB(),
    )
    assert (t.reply_text or "").strip()
    assert t.end_call is not True
    state = await memory.load_state("w34-429")
    assert state is not None
    assert state.slots.get("llm_degraded") is True
    assert llm.call_count >= 1


# ---------------------------------------------------------------------------
# Multi-loan
# ---------------------------------------------------------------------------


def test_multi_loan_highest_dpd_wins():
    rows = [
        {"status": "active", "days_past_due": 10, "branch": "A", "repay_amount": 1000},
        {"status": "active", "days_past_due": 75, "branch": "B", "repay_amount": 4500},
        {"status": "closed", "days_past_due": 200, "branch": "C", "repay_amount": 9000},
    ]
    winner, multi = select_winning_loan(rows)
    assert winner is not None
    assert winner["branch"] == "B"
    assert winner["days_past_due"] == 75
    assert multi is True

    single, multi_one = select_winning_loan([rows[0]])
    assert single is not None
    assert single["branch"] == "A"
    assert multi_one is False


def test_hydrate_sets_multi_loan_from_highest_dpd():
    borrower = BorrowerRecord(
        borrower_id=BORROWER,
        identity={"name": "रमेश"},
        loan={"days_past_due": 10, "dpd": 10, "branch": "A", "repay_amount": 1000},
        loans=[
            {"status": "active", "days_past_due": 10, "dpd": 10, "branch": "A"},
            {"status": "open", "days_past_due": 90, "dpd": 90, "branch": "कानपुर सिटी"},
        ],
    )
    state = hydrate_from_borrower(new_conversation_state("w34-ml", TENANT, BORROWER), borrower)
    assert state.slots.get("multi_loan") is True
    assert int(state.slots.get("days_past_due") or 0) == 90
    assert state.slots.get("branch") == "कानपुर सिटी"


# ---------------------------------------------------------------------------
# Persist-async
# ---------------------------------------------------------------------------


def test_persist_async_upstash_off_critical_path():
    class UpstashMemoryStore:
        pass

    class CompositeMemoryStore:
        def __init__(self, inner):
            self._state = inner

    class OtherStore:
        pass

    assert _persist_off_critical_path(UpstashMemoryStore()) is True
    assert _persist_off_critical_path(CompositeMemoryStore(UpstashMemoryStore())) is True
    assert _persist_off_critical_path(InMemoryMemoryStore()) is False
    assert _persist_off_critical_path(OtherStore()) is False
    assert _persist_off_critical_path(CompositeMemoryStore(InMemoryMemoryStore())) is False


# ---------------------------------------------------------------------------
# DEBT-038 / DEBT-043
# ---------------------------------------------------------------------------


def test_tts_segments_split_around_name():
    parts = segment_spoken_reply(
        "नमस्ते रमेश जी, किश्त बाकी है।",
        {"customer_name": "रमेश"},
    )
    assert parts[0] == "नमस्ते "
    assert parts[1] == "रमेश"
    assert "किश्त" in parts[2]
    static = split_template_static("नमस्ते {customer_name} जी, बात हो रही है।")
    assert static[0].startswith("नमस्ते")
    assert "जी" in static[1]
    assert "{customer_name}" not in "".join(static)


def test_consent_enum_forms_map_to_yes():
    profile = get_tenant_profile(TENANT)
    assert profile is not None
    for uttered in ("haan bataiye", "haan boliye", "to"):
        cmds = coerce_consent([], "plo_consent_2min", uttered, profile=profile)
        assert cmds and cmds[0].command == "set_slot"
        assert cmds[0].name == "plo_consent_2min"
        assert cmds[0].value == "yes", uttered
    already = [Command(command="set_slot", name="plo_consent_2min", value="maybe")]
    kept = coerce_consent(already, "plo_consent_2min", "haan bataiye", profile=profile)
    assert kept[0].value == "maybe"
