"""G2 — bare nahi at plo_payment_intent routes to refuse push att1.

Locking replay of console session db3037ad01ef (paisalo/postdue1):
  opener → identity → nahi #1 → refuse push attempt 1
  (not ev2 cue_agree, not ESCALATED_UNCLEAR).
"""

from __future__ import annotations

import json
import logging

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.evidence_scorer import score_evidence
from app.engine.retrieval import clear_retrieval_cache
from app.engine.scripted_coercions import coerce_payment_refusal, is_bare_negation
from app.engine.tenant_profile import clear_tenant_profile_cache, get_tenant_profile
from app.engine.tracker import new_conversation_state
from app.engine.turn import handle_turn
from app.flows.loader import reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest

CALL_DATE = "2026-08-15"
TENANT = "paisalo"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("SCRIPTED_CATALOG_ROUTING", "true")
    monkeypatch.setenv("CALL_WINDOW_START", "00:00")
    monkeypatch.setenv("CALL_WINDOW_END", "23:59")
    monkeypatch.setenv("TEST_PLO_SCENARIO", "postdue1")
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
    def __init__(self):
        self.call_count = 0

    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def complete(self, system, user, *, json_only=True, **kw) -> str:
        self.call_count += 1
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


def test_g2_bare_nahi_is_negation_not_agree():
    profile = get_tenant_profile(TENANT)
    assert is_bare_negation("nahi", profile) is True
    assert is_bare_negation("नहीं", profile) is True
    assert is_bare_negation("nahi ji", profile) is True
    assert is_bare_negation("nahi aaj nahi kal karunga", profile) is False
    cmds, fired, via, cls = coerce_payment_refusal(
        [], "plo_payment_intent", "nahi", profile=profile
    )
    assert fired is True
    assert via == "cue"
    assert cls == "unwilling"
    assert any(
        c.command == "set_slot" and c.name == "plo_payment_intent" and c.value == "refused"
        for c in cmds
    )
    score = score_evidence(
        transcript="nahi",
        state=new_conversation_state("g2-ev", TENANT, "b"),
        profile=profile,
        llm_calls=0,
        commands=[],
        last_spoken_reply="",
        echo=False,
        awaited_slot="plo_payment_intent",
    )
    assert score["evidence"] == 2
    assert score["evidence_reason"] == "cue_refuse"


@pytest.mark.asyncio
async def test_g2_db3037_nahi_one_routes_refuse_push_att1(caplog):
    memory = InMemoryMemoryStore()
    call_id = "db3037ad01ef"
    llm = _ScriptedLLM()
    with caplog.at_level(logging.INFO, logger="app.engine.turn_decision_log"):
        t0 = await handle_turn(
            _req(call_id, ""), memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB()
        )
        t1 = await handle_turn(
            _req(call_id, "हाँ, मैं रमेश बोल रहा हूँ।"),
            memory=memory,
            llm=llm,
            tools=FakeToolClient(),
            kb=_EmptyKB(),
        )
        t2 = await handle_turn(
            _req(call_id, "nahi"),
            memory=memory,
            llm=llm,
            tools=FakeToolClient(),
            kb=_EmptyKB(),
        )
    assert t0.reply_text
    assert t1.reply_text
    assert t2.reply_id == "plo_pd1_refuse"
    assert "NPA" in (t2.reply_text or "") or "भुगतान" in (t2.reply_text or "")
    assert t2.disposition != "ESCALATED_UNCLEAR"
    rows = [
        r.getMessage()
        for r in caplog.records
        if "turn_decision" in r.getMessage() and "nahi" in r.getMessage()
    ]
    assert rows
    payload = json.loads(rows[-1][rows[-1].find("{") :])
    guards = payload.get("guards") or payload
    assert guards.get("evidence_reason") == "cue_refuse"
    assert guards.get("disposition") != "ESCALATED_UNCLEAR"
