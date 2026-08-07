"""F1/F2 — reason_catchall coercion + 06434c15 golden replay."""

from __future__ import annotations

import json
import logging

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.retrieval import clear_retrieval_cache
from app.engine.scripted_coercions import coerce_reason_catchall, run_coercion_chain
from app.engine.tenant_profile import clear_tenant_profile_cache, get_tenant_profile
from app.engine.turn import handle_turn
from app.flows.loader import reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.schemas.command import Command


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("SCRIPTED_CATALOG_ROUTING", "true")
    monkeypatch.setenv("SOT_DIGRESSION", "false")
    monkeypatch.setenv("CALL_WINDOW_START", "00:00")
    monkeypatch.setenv("CALL_WINDOW_END", "23:59")
    monkeypatch.setenv("TEST_SOT_SCENARIO", "pre")
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

    async def complete(self, system: str, user: str, *, json_only: bool = True, **kw) -> str:
        self.call_count += 1
        if self.call_count <= len(self._responses):
            return self._responses[self.call_count - 1]
        return "[]"


def test_reason_catchall_fills_garbled_reason():
    profile = get_tenant_profile("salary_on_time")
    assert profile is not None
    transcript = "दिक्कत कुछ नहीं आ रही है, अभी है नहीं पे।"
    cmds, fired = coerce_reason_catchall(
        [Command(command="clarify")],
        "sot_payment_problem",
        transcript,
        profile=profile,
    )
    assert fired is True
    assert any(
        c.command == "set_slot"
        and c.name == "sot_payment_problem"
        and c.value == transcript
        for c in cmds
    )
    assert not any(c.command == "clarify" for c in cmds)


def test_reason_catchall_skips_blank():
    profile = get_tenant_profile("salary_on_time")
    assert profile is not None
    cmds, fired = coerce_reason_catchall(
        [Command(command="clarify")],
        "sot_payment_problem",
        "   ",
        profile=profile,
    )
    assert fired is False
    assert cmds[0].command == "clarify"


def test_reason_catchall_after_refusal_not_run():
    """Clean refusal still routes refusal first — catchall must not steal the turn."""
    profile = get_tenant_profile("salary_on_time")
    assert profile is not None
    cmds, meta = run_coercion_chain(
        [],
        "sot_payment_intent",
        "ठीक है, तो मैं पेमेंट नहीं दे पाऊंगा।",
        profile=profile,
        on_rails=True,
        blank_transcript=False,
    )
    assert meta.get("refusal_matched_via") in {"cue", "regex", "inability"}
    assert any(
        c.command == "set_slot" and c.name == "sot_payment_intent" and c.value == "refused"
        for c in cmds
    )
    assert not any(c.name == "sot_payment_problem" for c in cmds if c.command == "set_slot")


@pytest.mark.asyncio
async def test_06434c15_reason_fills_no_escalate(caplog):
    """Golden replay of live call 06434c15 t1–t6: t4 fills reason, no ESCALATED_UNCLEAR."""
    caplog.set_level(logging.INFO, logger="app.engine.turn_decision_log")
    memory = InMemoryMemoryStore()
    call_id = "06434c15-golden"
    # Reproduce live LLM bugs: text-not-value on slots; empty/clarify at reason.
    llm = _ScriptedLLM(
        [
            [],  # opener
            [
                {
                    "command": "set_slot",
                    "name": "sot_identity_response",
                    "text": "confirmed",
                    "flow": "sot_opener",
                },
                {"command": "start_flow", "flow": "sot_obj_where_from"},
            ],
            [{"command": "start_flow", "flow": "sot_obj_where_from"}],
            [
                {
                    "command": "set_slot",
                    "name": "sot_payment_intent",
                    "text": "मैं पेमेंट नहीं दे पाऊंगा",
                    "flow": "sot_offer_pre_closure",
                },
                {"command": "start_flow", "flow": "sot_push"},
            ],
            # t4 live bug: set_slot with text only → rejected → clarify without catchall
            [
                {
                    "command": "set_slot",
                    "name": "sot_payment_problem",
                    "text": "दिक्कत कुछ नहीं आ रही है, अभी है नहीं पे।",
                }
            ],
            [
                {
                    "command": "set_slot",
                    "name": "sot_payment_problem",
                    "text": "पेमेंट नहीं करने के लिए जो है ना अभी ज्यादा।",
                },
                {"command": "start_flow", "flow": "sot_obj_wont_pay"},
            ],
        ]
    )

    async def _run(transcript: str):
        return await handle_turn(
            TurnRequest(
                call_id=call_id,
                tenant_id="salary_on_time",
                borrower_id="sot_test_borrower",
                transcript=transcript,
                turn_meta={"force_flow": "sot_opener", "call_date": "2026-06-25"},
            ),
            memory=memory,
            kb=_EmptyKB(),
            llm=llm,
            tools=FakeToolClient(),
        )

    await _run("")
    await _run("हाँ, मैं ऋषभ बोल रहा हूँ। कौन बोल रहे हैं?")
    await _run("आप बोल कौन रहे हैं?")
    r3 = await _run("ठीक है, तो मैं पेमेंट नहीं दे पाऊंगा।")
    state = await memory.load_state(call_id)
    assert state.slots.get("sot_payment_intent") == "refused"
    assert r3.reply_id == "sot_ask_reason"

    reason_tx = "दिक्कत कुछ नहीं आ रही है, अभी है नहीं पे।"
    r4 = await _run(reason_tx)
    state = await memory.load_state(call_id)
    assert state.slots.get("sot_payment_problem") == reason_tx
    # Advanced off ask_reason → push collect (pick_push path)
    assert state.slots.get("last_question_slot") == "sot_payment_intent_2" or r4.reply_id in {
        "sot_push",
        "sot_push_tp",
    }
    assert "ESCALATED_UNCLEAR" not in (r4.disposition or "")

    r5 = await _run("पेमेंट नहीं करने के लिए जो है ना अभी ज्यादा।")
    assert "ESCALATED_UNCLEAR" not in (r5.disposition or "")
