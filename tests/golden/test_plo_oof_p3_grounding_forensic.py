"""PLO-OOF P3 — Grounding label forensic (turn 5, session fb6a0f02272048dda3c85d0162aa7b32).

Forensic finding from the live PREDUE call (H1-CLOSE redial):

  Turn 5 transcript: "भुगतान कब तक कितना है मेरा?" (How much is my payment and by when?)
  Raw LLM respond text: "आपका भुगतान 13-08-2026 तक 4500 rupaye है।"
  Guard decision:       grounding_result = "swapped"
  Spoken reply_id:      plo_reask_intent
  final_text_len:       176

  The date "13-08-2026" is NOT in the hydrated slot values (the predue
  borrower has dpd=-5; the due date is not hydrated as a slot), so the
  grounding guard correctly swapped the ENTIRE respond text to the tenant's
  ``unknown_info_reply``. The spoken draft = unknown_info_reply + reask
  (~176 chars), NOT the facts + reask (~70 chars).

Verdict: NEITHER a label bug NOR a swap bypass. The ``grounding_result:
  "swapped"`` label is accurate; the facts did NOT speak; the borrower heard
  the compliance-safe unknown_info_reply + the collect re-ask.

This test LOCKS that behavior so a future regression (e.g. facts speaking
on an ungrounded respond) is caught.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.respond_guard import ground_respond_text
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


# ---------------------------------------------------------------------------
# Unit: ground_respond_text — the guard itself
# ---------------------------------------------------------------------------


def test_p3_ground_pass_when_all_numerics_in_slots():
    """All numeric tokens present in slot values → pass (facts speak)."""
    slots = {"repay_amount": 4500, "due_date": "2026-08-13"}
    text = "आपका भुगतान 2026-08-13 तक 4500 rupaye है।"
    grounded, result = ground_respond_text(text, slots, "माफ़ कीजिए जानकारी नहीं है।")
    assert result == "pass"
    assert grounded == text  # facts speak verbatim


def test_p3_ground_swaps_when_date_not_in_slots():
    """A date not in slot values → swap to unknown_info_reply (facts do NOT speak).

    This is the turn-5 forensic case: "13-08-2026" is not a hydrated slot value
    for the predue borrower, so the entire respond text is replaced.
    """
    slots = {"repay_amount": 4500, "days_past_due": -5}  # no due_date slot
    text = "आपका भुगतान 13-08-2026 तक 4500 rupaye है।"
    unknown = "माफ़ कीजिए Ramesh जी, यह जानकारी अभी मेरे पास उपलब्ध नहीं है।"
    grounded, result = ground_respond_text(text, slots, unknown)
    assert result == "swapped"
    assert grounded == unknown
    # The facts must NOT survive into the grounded text.
    assert "13-08-2026" not in grounded
    assert "4500" not in grounded


def test_p3_ground_swaps_when_amount_not_in_slots():
    slots = {"days_past_due": -5}
    text = "आपका भुगतान 4500 rupaye है।"
    unknown = "माफ़ कीजिए जानकारी नहीं है।"
    grounded, result = ground_respond_text(text, slots, unknown)
    assert result == "swapped"
    assert grounded == unknown
    assert "4500" not in grounded


def test_p3_ground_empty_respond_swaps():
    slots = {"repay_amount": 4500}
    unknown = "माफ़ कीजिए जानकारी नहीं है।"
    grounded, result = ground_respond_text("", slots, unknown)
    assert result == "swapped"
    assert grounded == unknown


# ---------------------------------------------------------------------------
# Integration: turn-5 replay — facts do NOT speak; unknown_info_reply + reask does
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p3_turn5_replay_facts_swapped_unknown_reply_spoken(monkeypatch, caplog):
    """Replay turn 5: LLM respond with ungrounded date → unknown_info_reply + reask.

    The spoken text must be the unknown_info_reply concatenated with the short
    re-ask (plo_reask_intent), NOT the LLM's fact text. The turn_decision log
    must record grounding_result="swapped" so an auditor can distinguish the
    swap from a grounding pass.
    """
    monkeypatch.setenv("TEST_PLO_SCENARIO", "predue")
    get_settings.cache_clear()
    caplog.set_level(logging.INFO, logger="app.engine.turn_decision_log")
    memory = InMemoryMemoryStore()
    call_id = "plo-oof-p3-turn5"
    llm = _ScriptedLLM(
        [
            [],  # t1 greeting
            [{"command": "set_slot", "name": "plo_identity_response", "value": "confirmed"}],
            # t3: LLM emits a respond with an ungrounded date (13-08-2026).
            [
                {
                    "command": "respond",
                    "text": "आपका भुगतान 13-08-2026 तक 4500 rupaye है।",
                }
            ],
        ]
    )

    await _turn(memory, call_id, "", llm)  # t1
    await _turn(memory, call_id, "haan", llm)  # t2 identity → plo_predue ask
    r3 = await _turn(memory, call_id, "भुगतान कब तक कितना है मेरा?", llm)  # t3 respond

    # The spoken reply must NOT contain the ungrounded facts.
    assert "13-08-2026" not in (r3.reply_text or ""), (
        f"facts spoke despite swap: {r3.reply_text!r}"
    )
    # The spoken reply must contain the unknown_info_reply marker.
    assert "उपलब्ध नहीं" in (r3.reply_text or "") or "हेल्पलाइन" in (r3.reply_text or ""), (
        f"unknown_info_reply not spoken: {r3.reply_text!r}"
    )
    # The re-ask must follow (collect slot is still plo_payment_intent).
    assert "क्या आप यह भुगतान कर पाएंगे" in (r3.reply_text or "")

    # The turn_decision log must record grounding_result="swapped".
    decisions = [
        r for r in caplog.records if r.getMessage().startswith("turn_decision ")
    ]
    assert decisions, "no turn_decision logs captured"
    payload = json.loads(decisions[-1].getMessage().removeprefix("turn_decision "))
    assert payload["guards"]["grounding_result"] == "swapped"
    assert payload["guards"]["respond_fired"] is True
