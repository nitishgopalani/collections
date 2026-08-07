"""C1 / CP4 — t3 soft-refuse logs refusal_matched_via from coercion path."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.retrieval import clear_retrieval_cache
from app.engine.tenant_profile import clear_tenant_profile_cache
from app.engine.turn import handle_turn
from app.flows.loader import reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest

GUARD_OUT = (
    Path(__file__).resolve().parents[2] / "scripts" / "_p4_c1_t3_refusal_guards.json"
)


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


@pytest.mark.asyncio
async def test_t3_refusal_coercion_logs_matched_via_regex(caplog):
    """Session-style t3: 'नहीं, आज तो नहीं आ पाएगी।' → refusal via inability regex."""
    caplog.set_level(logging.INFO, logger="app.engine.turn_decision_log")
    memory = InMemoryMemoryStore()
    call_id = "p4-c1-t3-refusal"
    llm = _ScriptedLLM(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [],  # empty LLM — coercion must set refused
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
    await _run("haan Rishabh")
    r3 = await _run("नहीं, आज तो नहीं आ पाएगी।")

    state = await memory.load_state(call_id)
    assert state.slots.get("sot_payment_intent") == "refused"
    assert r3.reply_id == "sot_ask_reason"

    decisions = [
        r for r in caplog.records if r.getMessage().startswith("turn_decision ")
    ]
    payload = json.loads(decisions[-1].getMessage().removeprefix("turn_decision "))
    guards = payload["guards"]
    assert guards["refusal_matched_via"] == "regex"
    assert payload["transcript"].startswith("नहीं")
    assert "set_slot:sot_payment_intent=refused" in payload["commands"]

    GUARD_OUT.write_text(
        json.dumps(
            {
                "transcript": payload["transcript"],
                "commands": payload["commands"],
                "reply_id": payload["reply_id"],
                "question_slot": payload["question_slot"],
                "guards": guards,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_refusal_matched_via_cue_when_cue_hits():
    """Cue path wins when an intent_refusal cue is present."""
    from app.engine.scripted_coercions import coerce_payment_refusal
    from app.engine.tenant_profile import get_tenant_profile

    profile = get_tenant_profile("salary_on_time")
    assert profile is not None
    cmds, fired, via = coerce_payment_refusal(
        [],
        "sot_payment_intent",
        "aaj nahi kar paunga",
        profile=profile,
    )
    assert fired is True
    assert via == "cue"
    assert any(c.command == "set_slot" and c.value == "refused" for c in cmds)
