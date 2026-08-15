"""L2-FIX locking golden — live call-1 24a836b7 replay.

Live (19922f2, 15 Aug ~13:28 IST) after which-EMI pay-ask:
  t4 "हाँ।" → willing confirm armed
  t5/t6 late-fee questions → repair=failed_confirm (pending stayed)
  t7 "ठीक है, कर दूँगा।" → stale-pending ev3 lock + hangup (no confirm step)

Expected after C2+C3:
  t4 हाँ still arms ONE confirm (collect is payment_intent).
  t5/t6 question-shape → answer fact_penalty_post, pending kept, zero repair ticks.
  t7 कर दूँगा → pending(willing) ev3 lock. One confirm total. No stale lock
  from a failed_confirm path.
"""

from __future__ import annotations

import json
import logging
import re

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.retrieval import clear_retrieval_cache
from app.engine.robustness import PENDING_CONFIRM_KEY, REPAIR_COUNTS_KEY
from app.engine.tenant_profile import clear_tenant_profile_cache
from app.engine.turn import handle_turn
from app.flows.loader import get_flow_set, reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest

CALL_DATE = "2026-08-15"
TENANT = "paisalo"
PAY_ASK_RE = re.compile(
    r"क्या आप.{0,48}भुगतान कर पाएंगे|क्या आप.{0,48}भुगतान कर पाएँगे"
    r"|क्या आप इस किश्त|कर पाएंगे\s*$|कर पाएँगे\s*$"
)


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
    return json.loads(msg[msg.find("{") :])


def _inner(guards: dict) -> dict:
    return guards.get("guards") or guards


@pytest.mark.asyncio
async def test_l2_call1_haan_latefee_kardunga_one_confirm_zero_repair(caplog):
    memory = InMemoryMemoryStore()
    call_id = "l2-call1-replay"
    llm = _ScriptedLLM(
        [
            [{"command": "start_flow", "flow": "plo_obj_which_emi_pd"}],
            [{"command": "compose", "fragments": ["fact_penalty_post"]}],
            [{"command": "compose", "fragments": ["fact_penalty_post"]}],
        ]
    )
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
            _req(call_id, "कौन सी ईएमआई?"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
        t4 = await handle_turn(
            _req(call_id, "हाँ।"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
        t5 = await handle_turn(
            _req(call_id, "लेट फील लग गई क्या?"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
        t6 = await handle_turn(
            _req(call_id, "नहीं, लेट फी लग गई है क्या मेरी?"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
        t7 = await handle_turn(
            _req(call_id, "ठीक है, कर दूँगा।"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )

    assert t1.reply_id == "plo_pd1_greet"
    assert t2.reply_text
    assert "नेहा" in (t2.reply_text or "")
    assert "बकाया" in (t2.reply_text or "")

    # C2: which-EMI is a real answer, no embedded pay-ask.
    assert t3.reply_id == "plo_obj_which_emi_pd"
    assert PAY_ASK_RE.search(t3.reply_text or "") is None
    assert any(tok in (t3.reply_text or "") for tok in ("ABF", "एबीएफ", "ड्यू", "देय"))

    g4 = _inner(_guards(caplog, "हाँ।"))
    assert g4.get("confirm_fragment_id") == "confirm_plo_payment_intent"
    assert g4.get("gate_verdict") == "downgrade"
    assert g4.get("repair_reason") is None

    g5 = _inner(_guards(caplog, "लेट फील"))
    assert g5.get("compose_fired") is True
    assert "fact_penalty_post" in (g5.get("compose_fragment_ids") or [])
    assert g5.get("repair_reason") is None
    assert "failed_confirm" not in str(g5.get("repair_reason") or "")

    g6 = _inner(_guards(caplog, "लेट फी लग गई है क्या मेरी"))
    assert g6.get("compose_fired") is True
    assert "fact_penalty_post" in (g6.get("compose_fragment_ids") or [])
    assert g6.get("repair_reason") is None

    g7 = _inner(_guards(caplog, "कर दूँगा"))
    assert g7.get("evidence") == 3
    assert g7.get("confirm_fragment_id") in (None, "")
    assert g7.get("repair_reason") is None
    assert t7.reply_id == "plo_pd1_assurance"
    assert "?" not in (t7.reply_text or "") and "क्या" not in (t7.reply_text or "")

    confirm_turns = 0
    for rec in caplog.records:
        msg = rec.getMessage()
        if "turn_decision" not in msg or call_id not in msg:
            continue
        payload = json.loads(msg[msg.find("{") :])
        inner = payload.get("guards") or {}
        if inner.get("confirm_fragment_id") == "confirm_plo_payment_intent":
            confirm_turns += 1
        assert inner.get("repair_reason") in (None, "")
    assert confirm_turns == 1, f"expected one confirm total, got {confirm_turns}"

    state = await memory.load_state(call_id)
    assert state is not None
    counts = state.slots.get(REPAIR_COUNTS_KEY) or {}
    assert int(counts.get("plo_payment_intent") or 0) == 0
    assert state.slots.get("plo_payment_intent") == "willing"
    assert PENDING_CONFIRM_KEY not in state.slots


def test_c2_objection_copy_no_embedded_pay_ask():
    """Objection replies must not double-ask; canonical re-ask owns collect."""
    flows = get_flow_set()
    objection_ids = [
        "plo_obj_which_emi_pd",
        "plo_obj_which_emi",
        "plo_obj_deny_loan_pd",
        "plo_obj_deny_loan",
        "plo_obj_will_you_pay_pd",
        "plo_obj_will_you_pay",
        "plo_obj_new_loan_pd",
        "plo_obj_will_not_pay",
        "plo_obj_multiple_loans",
        "plo_obj_assurance_pd",
        "plo_obj_npa_assurance",
    ]
    for rid in objection_ids:
        variants = flows.responses.get(rid) or []
        assert variants, f"missing {rid}"
        for v in variants:
            assert PAY_ASK_RE.search(v.text or "") is None, f"{rid} still pay-asks: {v.text!r}"

    which = (flows.responses.get("plo_obj_which_emi_pd") or [None])[0]
    assert which is not None
    assert "{product}" in which.text
    assert "{due_date}" in which.text
    assert "{days_past_due_words}" in which.text


def test_c4_assurance_is_statement_close():
    flows = get_flow_set()
    for rid in (
        "plo_pd1_assurance",
        "plo_pd2_assurance",
        "plo_pd1_assurance_date",
        "plo_pd2_assurance_date",
        "plo_pd3_assurance",
        "plo_pd3_assurance_date",
        "plo_npa_assurance_today",
        "plo_npa_assurance_date",
        "plo_obj_assurance_pd",
        "plo_obj_npa_assurance",
    ):
        variants = flows.responses.get(rid) or []
        assert variants, f"missing {rid}"
        for v in variants:
            text = v.text or ""
            assert "?" not in text, f"{rid} trailing ?: {text!r}"
            assert "क्या मैं" not in text and "आवश्यकता है" not in text, f"{rid}: {text!r}"
            assert "QR" in text or "क्यूआर" in text or "QR" in text
            assert "धन्यवाद" in text
