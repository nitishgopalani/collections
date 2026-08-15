"""L3-FIX locking golden — live 2f8f9f01 t4-t11 replay.

Live (f8c87b4, 15 Aug ~13:53 IST) PLO_POSTDUE3:
  t4 refuse → confirm_refused
  t5 restated refuse → ev3 refuse att1 (slot cleared)
  t6 refuse again → SECOND refuse confirm (bug)
  t7 lock → att2
  t8 "baad mein de dunga" → willing confirm (date never captured)
  t9-t11 "10 din baad" stuffed into plo_payment_intent → same confirm + repair

Expected after P1-P4:
  ONE refuse confirm total.
  t6 locked re-refusal → att2 directly, no second confirm.
  t7 skipped (would escalate close after att2).
  t8 vague later → ask_pay_date (no date, no willing confirm).
  t9 10 din baad → confirm_pay_date readback (Hindi spoken date).
  t10 restatement → ev3, assurance-with-date close, repair ticks = 0.
"""

from __future__ import annotations

import json
import logging
from datetime import date

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.command_gen import parse_and_validate_commands
from app.engine.evidence_scorer import confirms_pending_value
from app.engine.fragment_library import get_fragment, resolve_confirm_fragment
from app.engine.nlg import spoken_date_hindi
from app.engine.retrieval import clear_retrieval_cache
from app.engine.robustness import PENDING_CONFIRM_KEY, REPAIR_COUNTS_KEY
from app.engine.scripted_coercions import (
    _extract_committed_date,
    coerce_intent_date,
    is_vague_later,
    today_ist,
)
from app.engine.tenant_profile import clear_tenant_profile_cache, get_tenant_profile
from app.engine.turn import handle_turn
from app.flows.loader import reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest

CALL_DATE = "2026-08-15"
TENANT = "paisalo"
DATE_ISO = "2026-08-25"  # 10 days after CALL_DATE


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


# ---------------------------------------------------------------------------
# P1 / P2 units
# ---------------------------------------------------------------------------


def test_p1_enum_guard_rejects_prose_and_iso_on_intent():
    raw = json.dumps(
        [{"command": "set_slot", "name": "plo_payment_intent", "value": "बाद में दे दूँगा"}]
    )
    result = parse_and_validate_commands(raw)
    assert not any(
        c.command == "set_slot" and c.name == "plo_payment_intent" for c in result.commands
    )
    assert any("slot_enum_violation" in r for r in result.rejections)

    raw2 = json.dumps(
        [{"command": "set_slot", "name": "plo_payment_intent", "value": DATE_ISO}]
    )
    result2 = parse_and_validate_commands(raw2)
    assert any("slot_enum_violation" in r for r in result2.rejections)

    ok = parse_and_validate_commands(
        json.dumps([{"command": "set_slot", "name": "plo_payment_intent", "value": "willing"}])
    )
    assert any(
        c.command == "set_slot" and c.value == "willing" for c in ok.commands
    )


def test_p2_relative_date_and_vague_later():
    today = date(2026, 8, 15)
    assert _extract_committed_date("10 दिन बाद भेजूँगा।", today=today) == DATE_ISO
    assert _extract_committed_date("kal de dunga", today=today) == "2026-08-16"
    assert _extract_committed_date("parso", today=today) == "2026-08-17"
    assert _extract_committed_date("agle hafte", today=today) == "2026-08-22"
    assert is_vague_later("अच्छा, मैं बाद में दे दूँगा।")
    assert is_vague_later("jald hi kar dunga")
    assert not is_vague_later("10 दिन बाद भेजूँगा।")

    profile = get_tenant_profile(TENANT)
    cmds, fired, ask = coerce_intent_date(
        [], "plo_payment_intent", "10 दिन बाद भेजूँगा।", profile=profile, today=today
    )
    assert fired and ask is None
    slots = {c.name: c.value for c in cmds if c.command == "set_slot"}
    assert slots["plo_payment_intent"] == "willing"
    assert slots["committed_date"] == DATE_ISO

    cmds2, fired2, ask2 = coerce_intent_date(
        [], "plo_payment_intent", "अच्छा, मैं बाद में दे दूँगा।",
        profile=profile, today=today,
    )
    assert fired2 and ask2 == "concrete"
    assert not any(c.name == "committed_date" for c in cmds2 if c.command == "set_slot")

    cmds3, fired3, ask3 = coerce_intent_date(
        [], "plo_payment_intent", "agle mahine", profile=profile, today=today
    )
    assert fired3 and ask3 == "nearer"


def test_p3_confirm_pay_date_fragment_and_spoken():
    assert get_fragment(TENANT, "confirm_pay_date")
    assert get_fragment(TENANT, "ask_pay_date")
    fid = resolve_confirm_fragment(
        TENANT, "plo_payment_intent", "willing", committed_date=DATE_ISO
    )
    assert fid == "confirm_pay_date"
    spoken = spoken_date_hindi(DATE_ISO)
    assert "August" in spoken
    assert spoken != DATE_ISO


def test_p4_date_restatement_confirms_pending():
    profile = get_tenant_profile(TENANT)
    today = date(2026, 8, 15)
    assert confirms_pending_value(
        "मैं कहा ना 10 दिन बाद दे दूंगा मैं।",
        profile,
        "willing",
        pending_date=DATE_ISO,
        today=today,
    )
    assert confirms_pending_value(
        "नहीं कर पाऊंगा अभी।",
        profile,
        "refused",
    )


def test_today_ist_pins_call_date():
    assert today_ist(CALL_DATE) == date(2026, 8, 15)


@pytest.mark.asyncio
async def test_l3_2f8f9f01_t4_t11_one_refuse_confirm_date_close(caplog):
    memory = InMemoryMemoryStore()
    call_id = "l3-2f8f9f01-replay"
    llm = _ScriptedLLM(
        [
            [{"command": "start_flow", "flow": "plo_obj_which_emi_pd"}],
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
            _req(call_id, "नहीं नहीं नहीं कर पाऊंगा मैं।"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
        t5 = await handle_turn(
            _req(call_id, "नहीं कर पाऊंगा अभी।"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
        t6 = await handle_turn(
            _req(call_id, "नहीं, मैं नहीं कर पाऊंगा।"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
        # t7 live would escalate close after att2 — skip so the date path can run.
        t8 = await handle_turn(
            _req(call_id, "अच्छा, मैं बाद में दे दूँगा।"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
        t9 = await handle_turn(
            _req(call_id, "हाँ, 10 दिन बाद भेजूँगा।"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
        t10 = await handle_turn(
            _req(call_id, "नहीं नहीं, 10 दिन बाद दे दूँगा।"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )

    assert t1.reply_id == "plo_pd3_greet"
    assert t2.reply_text
    assert t3.reply_id == "plo_obj_which_emi_pd"

    g4 = _inner(_guards(caplog, "नहीं नहीं नहीं कर पाऊंगा"))
    assert g4.get("confirm_fragment_id") == "confirm_plo_payment_intent_refused"
    assert g4.get("gate_verdict") == "downgrade"
    assert g4.get("repair_reason") is None

    g5 = _inner(_guards(caplog, "नहीं कर पाऊंगा अभी"))
    assert g5.get("evidence") == 3
    assert g5.get("confirm_fragment_id") in (None, "")
    assert t5.reply_id == "plo_pd3_refuse"

    g6 = _inner(_guards(caplog, "नहीं, मैं नहीं कर पाऊंगा"))
    assert g6.get("confirm_fragment_id") in (None, "")
    assert g6.get("gate_verdict") != "downgrade"
    assert t6.reply_id == "plo_pd3_refuse"

    g8 = _inner(_guards(caplog, "बाद में दे दूँगा"))
    assert g8.get("confirm_fragment_id") in (None, "")
    assert "ask_pay_date" in (g8.get("compose_fragment_ids") or [])
    assert "तारीख़" in (t8.reply_text or "") or "तारीख" in (t8.reply_text or "")

    g9 = _inner(_guards(caplog, "10 दिन बाद भेजूँगा"))
    assert g9.get("confirm_fragment_id") == "confirm_pay_date"
    spoken = spoken_date_hindi(DATE_ISO)
    assert spoken in (t9.reply_text or "")
    assert "रुपये" in (t9.reply_text or "")

    g10 = _inner(_guards(caplog, "10 दिन बाद दे दूँगा"))
    assert g10.get("evidence") == 3
    assert g10.get("repair_reason") is None
    assert t10.reply_id == "plo_pd3_assurance_date"
    assert spoken in (t10.reply_text or "")
    assert "?" not in (t10.reply_text or "")

    refuse_confirms = 0
    date_confirms = 0
    for rec in caplog.records:
        msg = rec.getMessage()
        if "turn_decision" not in msg or call_id not in msg:
            continue
        payload = json.loads(msg[msg.find("{") :])
        inner = payload.get("guards") or {}
        fid = inner.get("confirm_fragment_id")
        if fid == "confirm_plo_payment_intent_refused":
            refuse_confirms += 1
        if fid == "confirm_pay_date":
            date_confirms += 1
        assert inner.get("repair_reason") in (None, "")
        assert fid != "confirm_plo_payment_intent"
    assert refuse_confirms == 1, f"expected one refuse confirm, got {refuse_confirms}"
    assert date_confirms == 1, f"expected one date confirm, got {date_confirms}"

    state = await memory.load_state(call_id)
    assert state is not None
    counts = state.slots.get(REPAIR_COUNTS_KEY) or {}
    assert int(counts.get("plo_payment_intent") or 0) == 0
    assert state.slots.get("committed_date") == DATE_ISO
    assert PENDING_CONFIRM_KEY not in state.slots
