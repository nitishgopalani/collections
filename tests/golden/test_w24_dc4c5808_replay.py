"""W2-4 E1/E2/E3 — full 4-turn replay of live session dc4c5808.

Live (bd98fb0, 15 Aug 12:00 IST) failed:
  t3 "कौन सी एमआई?" → start_flow plo_obj_which_emi classified escalate,
     downgraded, EMI never answered, phantom _pending_confirm planted.
  t4 "हाँ। ऑफिस कहाँ है?" → evidence 3 via pending_confirm, willing written,
     predue ack, call ended.

Expected after E1+E2+E3:
  EMI answered, office answered, no phantom willing write.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.retrieval import clear_retrieval_cache
from app.engine.robustness import PENDING_CONFIRM_KEY
from app.engine.tenant_profile import clear_tenant_profile_cache
from app.engine.turn import handle_turn
from app.flows.loader import reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest

CALL_DATE = "2026-08-15"
OFFICE_RESPOND = (
    "माफ़ कीजिए रमेश जी, यह जानकारी अभी मेरे पास उपलब्ध नहीं है। "
    "सही जानकारी के लिए कृपया कानपुर सिटी ब्रांच या पैसालो हेल्पलाइन से संपर्क करें।"
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("SCRIPTED_CATALOG_ROUTING", "true")
    monkeypatch.setenv("CALL_WINDOW_START", "00:00")
    monkeypatch.setenv("CALL_WINDOW_END", "23:59")
    monkeypatch.setenv("TEST_PLO_SCENARIO", "predue")
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
    def __init__(self, turns):
        self._responses = [json.dumps(t, ensure_ascii=False) for t in turns]
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
        tenant_id="paisalo",
        channel="voice",
        locale="hi-IN",
        transcript=text,
        turn_meta={"force_flow": "plo_opener", "call_date": CALL_DATE},
    )


@pytest.mark.asyncio
async def test_dc4c5808_four_turn_replay(caplog):
    memory = InMemoryMemoryStore()
    call_id = "dc4c5808-replay"
    llm = _ScriptedLLM([
        [{"command": "set_slot", "name": "plo_identity_response", "text": "confirmed"}],
        [{"command": "start_flow", "flow": "plo_obj_which_emi"}],
        [
            {"command": "respond", "text": OFFICE_RESPOND},
            {"command": "set_slot", "name": "plo_payment_intent", "text": "willing"},
        ],
    ])

    with caplog.at_level(logging.INFO, logger="app.engine.turn_decision_log"):
        t1 = await handle_turn(
            _req(call_id, ""), memory=memory, llm=llm,
            tools=FakeToolClient(), kb=_EmptyKB(),
        )
        t2 = await handle_turn(
            _req(call_id, "हाँ, मैं रमेश बोल रहा हूँ।"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
        t3 = await handle_turn(
            _req(call_id, "कौन सी एमआई?"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
        t4 = await handle_turn(
            _req(call_id, "हाँ। ऑफिस कहाँ है?"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )

    assert t1.reply_text
    assert t2.reply_text
    # t3: EMI answer spoken (which-emi flow executed, not re-ask-only)
    assert t3.reply_text
    t3_low = t3.reply_text
    assert any(tok in t3_low for tok in ("किश्त", "लोन", "रुपये", "EMI", "एमआई")), t3.reply_text
    assert "plo_reask_intent" not in (t3.reply_text or "")

    state_after_t3 = await memory.load_state(call_id)
    assert state_after_t3 is not None
    assert PENDING_CONFIRM_KEY not in state_after_t3.slots, (
        "E2: which-emi execute must not plant _pending_confirm"
    )

    # t4: office answered, no phantom willing
    assert t4.reply_text
    assert any(tok in t4.reply_text for tok in ("ऑफिस", "जानकारी", "ब्रांच", "हेल्पलाइन")), t4.reply_text
    state = await memory.load_state(call_id)
    assert state is not None
    assert state.slots.get("plo_payment_intent") != "willing"
    assert t4.end_call is not True

    # Guards from t4 turn_decision: no money-state execute of willing
    t4_logs = [
        r.getMessage() for r in caplog.records
        if "turn_decision" in r.getMessage() and "ऑफिस कहाँ" in r.getMessage()
    ]
    assert t4_logs, "t4 turn_decision not logged"
    last = t4_logs[-1]
    assert '"evidence": 3' not in last or '"evidence_reason": "explicit_confirm"' not in last


@pytest.mark.asyncio
async def test_e2_downgrade_without_fragment_no_pending():
    """E2: a downgrade with confirm_fragment_id=None (end_call at ev 1)
    must NOT arm _pending_confirm."""
    memory = InMemoryMemoryStore()
    call_id = "e2-no-frag"
    llm = _ScriptedLLM([
        [{"command": "set_slot", "name": "plo_identity_response", "text": "confirmed"}],
        [{"command": "end_call"}],
    ])
    await handle_turn(
        _req(call_id, ""), memory=memory, llm=llm,
        tools=FakeToolClient(), kb=_EmptyKB(),
    )
    await handle_turn(
        _req(call_id, "हाँ, मैं रमेश बोल रहा हूँ।"),
        memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
    )
    await handle_turn(
        _req(call_id, "bas itna hi"),
        memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
    )
    state = await memory.load_state(call_id)
    assert state is not None
    assert PENDING_CONFIRM_KEY not in state.slots
