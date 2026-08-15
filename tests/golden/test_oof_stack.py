"""OOF-STACK L0 / L1 / L2.

  - PM → L0 zero-LLM
  - meri rashi? → L1 ack
  - aap kaun bol rahe hain → index recovery (one LLM call)
  - processing fee? → honest-miss (branch referral, not scope boundary)
  - 2nd politics → short binary, no ack
"""

from __future__ import annotations

import json
import logging

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.fragment_library import clear_fragment_cache
from app.engine.oof_stack import clear_irrelevant_topics_cache, match_l0
from app.engine.retrieval import clear_retrieval_cache
from app.engine.tenant_profile import clear_tenant_profile_cache
from app.engine.turn import handle_turn
from app.flows.loader import reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest

CALL_DATE = "2026-08-15"
TENANT = "paisalo"
BORROWER = "plo_test_borrower"
SCOPE_MARKERS = ("मैं सिर्फ़ पैसालो", "इस लोन के बारे में")
HONEST_MISS = "मेरे पास नहीं"
ACK_PREFIX = "आप शायद"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("SCRIPTED_CATALOG_ROUTING", "true")
    monkeypatch.setenv("CALL_WINDOW_START", "00:00")
    monkeypatch.setenv("CALL_WINDOW_END", "23:59")
    monkeypatch.setenv("TEST_PLO_SCENARIO", "postdue3")
    monkeypatch.setenv("COMMITMENT_GATE_ENFORCE", "true")
    clear_tenant_profile_cache()
    clear_irrelevant_topics_cache()
    clear_fragment_cache()
    get_settings.cache_clear()
    reload_flow_set()
    clear_retrieval_cache()
    yield
    clear_retrieval_cache()
    clear_irrelevant_topics_cache()
    clear_fragment_cache()
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


def _guards(caplog, needle: str) -> dict:
    rows = [
        r.getMessage()
        for r in caplog.records
        if "turn_decision" in r.getMessage() and needle in r.getMessage()
    ]
    assert rows, f"turn_decision missing needle={needle!r}"
    msg = rows[-1]
    start = msg.find("{")
    return json.loads(msg[start:])


async def _open(memory, call_id: str, llm) -> None:
    await handle_turn(
        _req(call_id, ""),
        memory=memory,
        llm=llm,
        tools=FakeToolClient(),
        kb=_EmptyKB(),
    )


def test_l0_packs_seeded():
    assert match_l0(TENANT, "PM ke baare mein batao") is not None
    assert match_l0(TENANT, "PM ke baare mein batao").subclass == "politics"
    assert match_l0(TENANT, "meri rashi") is None


@pytest.mark.asyncio
async def test_pm_is_l0_zero_llm(caplog):
    memory = InMemoryMemoryStore()
    llm = _ScriptedLLM()
    await _open(memory, "oof-pm", llm)
    with caplog.at_level(logging.INFO, logger="app.engine.turn_decision_log"):
        t = await handle_turn(
            _req("oof-pm", "PM ke baare mein batao"),
            memory=memory,
            llm=llm,
            tools=FakeToolClient(),
            kb=_EmptyKB(),
        )
    spoken = t.reply_text or ""
    assert llm.call_count == 0
    assert "राजनीति" in spoken
    assert ACK_PREFIX in spoken
    assert any(m in spoken for m in SCOPE_MARKERS)
    guards = _guards(caplog, "PM")
    inner = guards.get("guards") or guards
    assert inner.get("oof_layer") == "deterministic"
    assert inner.get("oof_class") == "irrelevant"
    assert int(inner.get("redirect_count") or 0) >= 1


@pytest.mark.asyncio
async def test_rashi_is_l1_ack(caplog):
    memory = InMemoryMemoryStore()
    llm = _ScriptedLLM(
        [
            {
                "commands": [
                    {
                        "command": "compose",
                        "fragments": ["irrelevant_redirect"],
                        "oof_class": "irrelevant",
                        "related": False,
                        "ack_text": "आप शायद राशि के बारे में पूछ रहे हैं",
                    }
                ],
                "related": False,
                "ack_text": "आप शायद राशि के बारे में पूछ रहे हैं",
                "oof_class": "irrelevant",
            }
        ]
    )
    await _open(memory, "oof-rashi", llm)
    before = llm.call_count
    with caplog.at_level(logging.INFO, logger="app.engine.turn_decision_log"):
        t = await handle_turn(
            _req("oof-rashi", "meri rashi?"),
            memory=memory,
            llm=llm,
            tools=FakeToolClient(),
            kb=_EmptyKB(),
        )
    spoken = t.reply_text or ""
    assert llm.call_count == before + 1
    assert "राशि" in spoken
    assert ACK_PREFIX in spoken
    guards = _guards(caplog, "rashi")
    inner = guards.get("guards") or guards
    assert inner.get("oof_layer") == "llm"
    assert inner.get("related") is False


@pytest.mark.asyncio
async def test_identity_index_recovery_one_llm_call(caplog):
    memory = InMemoryMemoryStore()
    llm = _ScriptedLLM(
        [
            {
                "commands": [
                    {
                        "command": "compose",
                        "fragments": ["irrelevant_redirect"],
                        "oof_class": "irrelevant",
                        "related": True,
                    }
                ],
                "related": True,
                "oof_class": "irrelevant",
            }
        ]
    )
    await _open(memory, "oof-id", llm)
    before = llm.call_count
    with caplog.at_level(logging.INFO, logger="app.engine.turn_decision_log"):
        t = await handle_turn(
            _req("oof-id", "aap kaun bol rahe hain"),
            memory=memory,
            llm=llm,
            tools=FakeToolClient(),
            kb=_EmptyKB(),
        )
    spoken = t.reply_text or ""
    assert llm.call_count == before + 1
    assert "पैसालो से बोल" in spoken
    assert HONEST_MISS not in spoken
    guards = _guards(caplog, "kaun bol")
    inner = guards.get("guards") or guards
    assert inner.get("recovered_via") == "index"
    assert inner.get("oof_class") == "call_context"


@pytest.mark.asyncio
async def test_processing_fee_honest_miss(caplog):
    memory = InMemoryMemoryStore()
    llm = _ScriptedLLM(
        [
            {
                "commands": [
                    {
                        "command": "compose",
                        "fragments": ["irrelevant_redirect"],
                        "oof_class": "irrelevant",
                        "related": True,
                    }
                ],
                "related": True,
                "oof_class": "irrelevant",
            }
        ]
    )
    await _open(memory, "oof-fee", llm)
    with caplog.at_level(logging.INFO, logger="app.engine.turn_decision_log"):
        t = await handle_turn(
            _req("oof-fee", "processing fee?"),
            memory=memory,
            llm=llm,
            tools=FakeToolClient(),
            kb=_EmptyKB(),
        )
    spoken = t.reply_text or ""
    assert HONEST_MISS in spoken or "ब्रांच" in spoken
    assert "मैं सिर्फ़ पैसालो" not in spoken
    guards = _guards(caplog, "processing")
    inner = guards.get("guards") or guards
    assert inner.get("related_miss") is True
    assert inner.get("recovered_via") is None


@pytest.mark.asyncio
async def test_second_politics_no_ack_short_binary():
    memory = InMemoryMemoryStore()
    llm = _ScriptedLLM()
    await _open(memory, "oof-pol2", llm)
    t1 = await handle_turn(
        _req("oof-pol2", "PM ke baare mein batao"),
        memory=memory,
        llm=llm,
        tools=FakeToolClient(),
        kb=_EmptyKB(),
    )
    assert ACK_PREFIX in (t1.reply_text or "")
    t2 = await handle_turn(
        _req("oof-pol2", "modi ji kya keh rahe"),
        memory=memory,
        llm=llm,
        tools=FakeToolClient(),
        kb=_EmptyKB(),
    )
    spoken = t2.reply_text or ""
    assert ACK_PREFIX not in spoken
    assert llm.call_count == 0
    assert spoken.strip()
    state = await memory.load_state("oof-pol2")
    assert state is not None
    assert int(state.slots.get("_redirect_count") or 0) >= 2
