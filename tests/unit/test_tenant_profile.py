"""P1 — TenantRuntimeProfile registry + fabricated test_generic happy path."""

import json

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.retrieval import clear_retrieval_cache
from app.engine.tenant_profile import (
    clear_tenant_profile_cache,
    get_tenant_profile,
)
from app.engine.turn import handle_turn
from app.flows.loader import reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest


@pytest.fixture(autouse=True)
def _reload(monkeypatch):
    # .env may pin COLLECTIONS_INCLUDE_TEST_FLOWS=false; conftest setdefault cannot override.
    monkeypatch.setenv("COLLECTIONS_INCLUDE_TEST_FLOWS", "true")
    clear_tenant_profile_cache()
    get_settings.cache_clear()
    reload_flow_set()
    clear_retrieval_cache()
    yield
    clear_tenant_profile_cache()
    clear_retrieval_cache()
    get_settings.cache_clear()


def test_salary_on_time_profile_loads_from_yaml():
    profile = get_tenant_profile("salary_on_time")
    assert profile is not None
    assert profile.tenant_id == "salary_on_time"
    assert profile.flow_prefix == "sot_"
    assert profile.objection_prefix == "sot_obj_"
    assert profile.respond_enabled is True
    assert "info@salaryontime.com" in profile.unknown_info_reply
    assert "sot_payment_intent" in profile.push_intent_slots
    assert "sot_opener" in profile.onrails_flows
    assert "human_handoff" in profile.blocked_commands
    assert profile.cues("willing")
    assert profile.cues("intent_refusal")
    # Loader unions refusal ∪ intent_refusal_extras (no YAML duplication).
    assert "nahi kar paunga" in profile.cues("intent_refusal")
    assert "aaj nahi kar" in profile.cues("intent_refusal")
    assert "नहीं हो पायegi" not in profile.cues("intent_refusal")
    assert profile.dispute_theme_flows["never_loan"] == "sot_obj_never_loan"
    assert profile.reason_slot == "sot_payment_problem"
    assert profile.coercion_chain == [
        "dispute",
        "willing",
        "refusal",
        "identity",
        "reversal",
        "confirm",
        "link",
        "reason_catchall",
    ]


def test_open_tenant_has_no_profile():
    assert get_tenant_profile("default") is None
    assert get_tenant_profile("") is None


def test_sot_force_flow_still_bypasses_identity_gate():
    """R6: SOT force_flow=sot_opener still skips identity_verification injection."""
    from app.engine.identity_gate import apply_identity_entry_gate
    from app.schemas.state import ConversationState

    state = ConversationState(
        call_id="r6-sot-force",
        tenant_id="salary_on_time",
        borrower_id="b1",
        slots={"_force_test_flow": "sot_opener", "borrower_name": "Rishabh"},
        flow_stack=[],
    )
    out = apply_identity_entry_gate(state)
    assert out.flow_stack == []
    assert not any(f.flow == "identity_verification" for f in out.flow_stack)


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
async def test_generic_tenant_happy_path(monkeypatch):
    """Fabricated test_generic tenant: 3-flow pack, no engine code edits required."""
    monkeypatch.setenv("CALL_WINDOW_START", "00:00")
    monkeypatch.setenv("CALL_WINDOW_END", "23:59")
    get_settings.cache_clear()
    assert get_tenant_profile("test_generic") is not None
    memory = InMemoryMemoryStore()
    call_id = "tg-happy"
    llm = _ScriptedLLM(
        [
            [],  # greeting
            [
                {"command": "set_slot", "name": "tg_continue", "value": "yes"},
                {"command": "start_flow", "flow": "tg_ask"},
            ],
            [
                {"command": "set_slot", "name": "tg_confirm", "value": "yes"},
                {"command": "start_flow", "flow": "tg_close"},
            ],
            [],  # close turn (hangup from flow)
        ]
    )
    kb = _EmptyKB()
    tools = FakeToolClient()

    async def _run(transcript: str):
        return await handle_turn(
            TurnRequest(
                call_id=call_id,
                tenant_id="test_generic",
                borrower_id="tg_borrower",
                transcript=transcript,
                turn_meta={"force_flow": "tg_opener", "call_date": "2026-06-25"},
            ),
            memory=memory,
            kb=kb,
            llm=llm,
            tools=tools,
        )

    r1 = await _run("")
    assert r1.reply_id == "tg_greeting"
    assert r1.end_call is False

    r2 = await _run("yes continue")
    state = await memory.load_state(call_id)
    assert any(f.flow == "tg_ask" for f in state.flow_stack)

    r3 = await _run("yes")
    state2 = await memory.load_state(call_id)
    # tg_close should be active or already hung up
    assert (
        any(f.flow == "tg_close" for f in state2.flow_stack)
        or r3.end_call is True
        or state2.slots.get("tg_call_closed")
        or state2.slots.get("end_call")
    )

    if not r3.end_call:
        r4 = await _run("")
        assert r4.end_call is True or "tg_thanks" in (r4.reply_id or "")
