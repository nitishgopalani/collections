"""PLO-OOF P2 — Re-ask laddering for plo_reask_intent.

attempt 1 = current copy ("क्या आप यह भुगतान कर पाएंगे?");
attempt 2 = short binary ask ("... — हाँ या नहीं?");
attempt 3 → existing repair-escalation path (max_slot_retries=2, unchanged).

Test: two unclears → binary re-ask spoken before any escalation.
"""

from __future__ import annotations

import json

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.nlg import max_attempt_for_reply
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


def test_p2_plo_reask_intent_has_attempt_indexed_variants():
    """YAML: plo_reask_intent declares attempt 1 (current) + attempt 2 (binary)."""
    from app.flows.loader import get_flow_set

    flows = get_flow_set()
    assert "plo_reask_intent" in flows.responses
    max_attempt = max_attempt_for_reply(flows, "plo_reask_intent")
    assert max_attempt == 2, f"expected max attempt 2, got {max_attempt}"
    variants = flows.responses["plo_reask_intent"]
    texts = [v.text for v in variants]
    # attempt 1 = current copy
    assert any("क्या आप यह भुगतान कर पाएंगे?" in t and v.attempt == 1 for t, v in zip(texts, variants))
    # attempt 2 = short binary ask
    assert any("हाँ या नहीं" in t and v.attempt == 2 for t, v in zip(texts, variants))


def test_p2_sot_push_retry_has_attempt_indexed_variants():
    """SOT reask laddering (trivial parity): sot_push_retry attempt 1 + 2 tagged."""
    from app.flows.loader import get_flow_set

    flows = get_flow_set()
    assert "sot_push_retry" in flows.responses
    max_attempt = max_attempt_for_reply(flows, "sot_push_retry")
    assert max_attempt == 2, f"expected max attempt 2, got {max_attempt}"
    variants = flows.responses["sot_push_retry"]
    assert any(v.attempt == 1 for v in variants)
    assert any(v.attempt == 2 for v in variants)


@pytest.mark.asyncio
async def test_p2_two_unclears_binary_reask_before_escalation(monkeypatch):
    """Two unclear answers → binary re-ask (attempt 2) spoken; no escalation yet.

    Turn 1 (initial ask): current copy (attempt 1). Borrower says "mmm" (unclear).
    Turn 2 (re-ask 1): binary ask (attempt 2). Borrower says "mmm" (unclear).
    Turn 3 (re-ask 2): binary ask (attempt 2, highest). No escalation yet.
    Turn 4 (re-ask 3): repair escalation fires (max_slot_retries=2).

    The binary re-ask must be spoken BEFORE any escalation.
    """
    monkeypatch.setenv("TEST_PLO_SCENARIO", "predue")
    get_settings.cache_clear()
    memory = InMemoryMemoryStore()
    call_id = "plo-oof-p2-laddering"
    # LLM returns empty every turn after identity — coercion + repair handle it.
    llm = _ScriptedLLM(
        [
            [],  # t1 greeting
            [{"command": "set_slot", "name": "plo_identity_response", "value": "confirmed"}],
            [],  # t3 unclear at plo_payment_intent
            [],  # t4 re-ask 1
            [],  # t5 re-ask 2
            [],  # t6 re-ask 3 → escalate
        ]
    )

    await _turn(memory, call_id, "", llm)  # t1 greeting

    # t2: identity confirmed → flow advances to plo_predue → initial ask at
    # plo_payment_intent (attempt 1, current copy).
    r2 = await _turn(memory, call_id, "haan", llm)
    assert r2.reply_id == "plo_reask_intent", r2.reply_id
    assert "क्या आप यह भुगतान कर पाएंगे?" in (r2.reply_text or "")
    assert "हाँ या नहीं" not in (r2.reply_text or "")
    assert not r2.end_call

    # t3: re-ask 1 → attempt 2 (binary ask). No escalation yet.
    r3 = await _turn(memory, call_id, "mmm", llm)
    assert r3.reply_id == "plo_reask_intent", r3.reply_id
    assert "हाँ या नहीं" in (r3.reply_text or ""), f"expected binary ask, got {r3.reply_text!r}"
    assert not r3.end_call, "escalation fired too early (before binary re-ask)"

    # t4: re-ask 2 → still attempt 2 (highest defined). No escalation yet.
    r4 = await _turn(memory, call_id, "mmm", llm)
    assert "हाँ या नहीं" in (r4.reply_text or "")
    assert not r4.end_call, "escalation fired before the third re-ask"

    # t5: re-ask 3 → repair escalation fires (max_slot_retries=2).
    r5 = await _turn(memory, call_id, "mmm", llm)
    assert r5.end_call, "expected repair escalation to end the call on the 3rd re-ask"
