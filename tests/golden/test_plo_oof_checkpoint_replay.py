"""PLO-OOF CHECKPOINT — replay of session fb6a0f02272048dda3c85d0162aa7b32 turns 1-8.

Live PREDUE call (H1-CLOSE redial) turn map (from brain turn_decision logs):

  T1 transcript=""                                   → plo_predue_greeting
  T2 transcript="ठीक है।"                          → plo_identity_ask (identity not yet confirmed)
  T3 transcript="ठीक है। हाँ ठीक है, कौन बोल रहे हो?"
     → identity confirmed → plo_predue → plo_reask_intent (asks plo_payment_intent)
  T4 transcript="और कौन सब कह रहे हैं?"             → respond (unknown_info_reply + reask)
  T5 transcript="भुगतान कब तक कितना है मेरा?"       → respond (facts SWAPPED → unknown_info_reply + reask)
  T6 transcript="ठीक है।"                          → BUG: clarify (plo_reask_intent)
  T7 transcript="मैं मैं।"                          → clarify (plo_reask_intent)
  T8 transcript="नहीं नहीं। ये नहीं कितना बहुत काम है।" → repair_escalation

P1 fix: the willing cue pack now includes "ठीक है" / "theek hai" + Devanagari forms, so a
"ठीक है" at plo_payment_intent coerces to willing → ack_willing (plo_predue_ack), NOT clarify.

This replay re-runs turns 1-8 with the P1 coercion active and asserts that T6 now advances to
the assurance path (plo_predue_ack) instead of re-asking (plo_reask_intent / clarify).
"""

from __future__ import annotations

import json

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.retrieval import clear_retrieval_cache
from app.engine.tenant_profile import clear_tenant_profile_cache
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
    """Mimics the live fb6a0f02 LLM: identity confirm, then respond (unknown_info),
    then a bare set_slot(plo_payment_intent, text="ठीक है") that the live guard
    rejected as an empty slot — which the P1 willing coercion now rescues.
    """

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


@pytest.mark.asyncio
async def test_cp_fb6a0f02_replay_turn6_advances_to_assurance(monkeypatch):
    """Replay fb6a0f02 turns 1-8; T6 "ठीक है" must now reach assurance, not clarify."""
    monkeypatch.setenv("TEST_PLO_SCENARIO", "predue")
    get_settings.cache_clear()
    memory = InMemoryMemoryStore()
    call_id = "cp-fb6a0f02-replay"
    llm = _ScriptedLLM(
        [
            [],  # T1 greeting (opener speaks plo_predue_greeting)
            # T2: LLM tries to set identity with text "ठीक है" (rejected empty slot
            # pre-P1; but coerce_identity rescues "ठीक" + "haan"-like cues).
            [{"command": "set_slot", "name": "plo_identity_response", "text": "ठीक है"}],
            # T3: LLM confirms identity.
            [{"command": "set_slot", "name": "plo_identity_response", "value": "confirmed"}],
            # T4: LLM emits respond (unknown_info_reply — OOC question).
            [{"command": "respond", "text": "माफ़ कीजिए, यह जानकारी अभी मेरे पास उपलब्ध नहीं है।"}],
            # T5: LLM emits respond with ungrounded facts (grounding swaps to unknown_info_reply).
            [
                {
                    "command": "respond",
                    "text": "आपका भुगतान 13-08-2026 तक 4500 rupaye है।",
                }
            ],
            # T6: LLM tries set_slot(plo_payment_intent, text="ठीक है") — rejected empty
            # slot pre-P1; P1 willing coercion now rescues → willing → assurance.
            [{"command": "set_slot", "name": "plo_payment_intent", "text": "ठीक है"}],
            # T7: LLM clarify (incomplete utterance).
            [{"command": "clarify", "reason": "incomplete utterance"}],
            # T8: LLM clarify again → repair escalation.
            [{"command": "clarify", "reason": "still incomplete"}],
        ]
    )

    # T1: greeting.
    r1 = await _turn(memory, call_id, "", llm)
    assert r1.reply_id == "plo_predue_greeting", r1.reply_id

    # T2: "ठीक है" — identity not yet confirmed; coerce_identity may rescue.
    r2 = await _turn(memory, call_id, "ठीक है।", llm)
    # Still in opener (identity ask or re-ask).
    assert r2.reply_id in {"plo_identity_ask", "plo_predue_greeting"}, r2.reply_id

    # T3: identity confirmed → plo_predue → plo_reask_intent (asks plo_payment_intent).
    r3 = await _turn(memory, call_id, "ठीक है। हाँ ठीक है, कौन बोल रहे हो?", llm)
    assert r3.reply_id == "plo_reask_intent", r3.reply_id

    # T4: OOC question → respond (unknown_info_reply + reask).
    r4 = await _turn(memory, call_id, "और कौन सब कह रहे हैं?", llm)
    assert "उपलब्ध नहीं" in (r4.reply_text or "") or "हेल्पलाइन" in (r4.reply_text or "")

    # T5: facts question → respond swapped to unknown_info_reply + reask.
    r5 = await _turn(memory, call_id, "भुगतान कब तक कितना है मेरा?", llm)
    assert "13-08-2026" not in (r5.reply_text or ""), "facts must not speak (swap)"
    assert "उपलब्ध नहीं" in (r5.reply_text or "") or "हेल्पलाइन" in (r5.reply_text or "")

    # T6: "ठीक है" at plo_payment_intent → P1 coercion → willing → assurance.
    # THIS IS THE CHECKPOINT ASSERTION: turn 6 now advances to assurance (plo_predue_ack),
    # NOT clarify (plo_reask_intent).
    r6 = await _turn(memory, call_id, "ठीक है।", llm)
    state = await memory.load_state(call_id)
    assert state.slots.get("plo_payment_intent") == "willing", (
        f"P1 coercion did not rescue 'ठीक है' to willing: {state.slots.get('plo_payment_intent')!r}"
    )
    assert r6.reply_id == "plo_predue_ack", (
        f"T6 must advance to assurance (plo_predue_ack), got {r6.reply_id!r}"
    )
    # The assurance reply must thank / acknowledge (not re-ask).
    assert "बहुत अच्छा" in (r6.reply_text or "") or "अच्छा" in (r6.reply_text or ""), (
        f"assurance reply missing ack: {r6.reply_text!r}"
    )
