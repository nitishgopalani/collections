"""W2-5 compose few-shots + W2-4b LLM-diet (D1/D2/D3).

W2-5 (prompt + few-shots only):
  complaint -> compose ack+grievance + oof_class=complaint + complaint_raised
  weather   -> oof_class=irrelevant + redirect_count >= 1
  facts     -> compose fragment (not hatch)
  hatch     <5% on the mocked OOF replay

W2-4b:
  D1 cue-hit skip (identity/willing; question-shape never skips)
  D2 in-session classification cache
  D3 state-scoped catalog + scope_miss accept + A/B token table
"""

from __future__ import annotations

import json
import logging

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.catalog import (
    build_scoped_catalog,
    estimate_catalog_tokens,
    tenant_flow_catalog,
)
from app.engine.command_gen import (
    COMPOSE_FEW_SHOTS,
    build_system_prompt,
    parse_and_validate_commands,
)
from app.engine.retrieval import clear_retrieval_cache
from app.engine.scripted_coercions import cue_hit_pack
from app.engine.tenant_profile import clear_tenant_profile_cache, get_tenant_profile
from app.engine.tracker import new_conversation_state
from app.engine.turn import handle_turn
from app.flows.loader import get_flow_set, reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.schemas.state import Frame

CALL_DATE = "2026-08-15"
TENANT = "paisalo"


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
        self.last_system = ""

    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def complete(self, system: str, user: str, *, json_only: bool = True, **kw) -> str:
        self.call_count += 1
        self.last_system = system
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
    start = msg.find("{")
    return json.loads(msg[start:])


# ---------------------------------------------------------------------------
# W2-5 prompt + parse
# ---------------------------------------------------------------------------


def test_system_prompt_includes_compose_fewshots():
    prompt = build_system_prompt(
        "2026-08-15",
        respond_enabled=True,
        catalog_mode=True,
        unknown_info_reply="unknown",
    )
    assert "ack_neutral" in prompt
    assert "fact_grievance" in prompt
    assert "irrelevant_redirect" in prompt
    assert "fact_branch" in prompt
    assert "fact_caller_identity" in prompt
    assert "aap kaun bol rahe hain" in prompt
    assert '"who_are_you"' not in COMPOSE_FEW_SHOTS
    assert '"fact_agent_intro"' not in COMPOSE_FEW_SHOTS
    assert "oof_class" in prompt
    assert "mausam kaisa hai" in prompt
    assert COMPOSE_FEW_SHOTS in prompt


def test_parse_compose_complaint_sets_oof_class():
    raw = json.dumps(
        [
            {
                "command": "compose",
                "fragments": ["ack_neutral", "fact_grievance"],
                "oof_class": "complaint",
            }
        ]
    )
    result = parse_and_validate_commands(raw, respond_enabled=True)
    assert result.oof_class == "complaint"
    assert result.commands[0].command == "compose"
    assert result.commands[0].fragments == ["ack_neutral", "fact_grievance"]


def test_parse_compose_irrelevant_sets_oof_class():
    raw = json.dumps(
        [
            {
                "command": "compose",
                "fragments": ["irrelevant_redirect"],
                "oof_class": "irrelevant",
            }
        ]
    )
    result = parse_and_validate_commands(raw, respond_enabled=True)
    assert result.oof_class == "irrelevant"


# ---------------------------------------------------------------------------
# W2-5 turn-level: weather redirect + complaint + hatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_weather_fixture_increments_redirect_count(caplog):
    memory = InMemoryMemoryStore()
    call_id = "w25-weather"
    llm = _ScriptedLLM(
        [
            [
                {
                    "command": "compose",
                    "fragments": ["irrelevant_redirect"],
                    "oof_class": "irrelevant",
                }
            ]
        ]
    )
    with caplog.at_level(logging.INFO, logger="app.engine.turn_decision_log"):
        await handle_turn(
            _req(call_id, ""), memory=memory, llm=llm,
            tools=FakeToolClient(), kb=_EmptyKB(),
        )
        await handle_turn(
            _req(call_id, "haan, main Ramesh bol raha hoon"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
        t3 = await handle_turn(
            _req(call_id, "mausam kaisa hai?"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
    assert t3.reply_text
    guards = _guards(caplog, "mausam")
    inner = guards.get("guards") or guards
    assert inner.get("oof_class") == "irrelevant"
    assert int(inner.get("redirect_count") or 0) >= 1
    assert inner.get("unrelated_redirect") is True
    assert inner.get("escape_hatch_used") is not True


@pytest.mark.asyncio
async def test_complaint_compose_sets_complaint_raised(caplog):
    memory = InMemoryMemoryStore()
    call_id = "w25-complaint"
    llm = _ScriptedLLM(
        [
            [
                {
                    "command": "compose",
                    "fragments": ["ack_neutral", "fact_grievance"],
                    "oof_class": "complaint",
                }
            ]
        ]
    )
    with caplog.at_level(logging.INFO, logger="app.engine.turn_decision_log"):
        await handle_turn(
            _req(call_id, ""), memory=memory, llm=llm,
            tools=FakeToolClient(), kb=_EmptyKB(),
        )
        await handle_turn(
            _req(call_id, "haan, main Ramesh bol raha hoon"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
        t3 = await handle_turn(
            _req(call_id, "yeh company bekar hai"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
    assert t3.reply_text
    guards = _guards(caplog, "bekar")
    inner = guards.get("guards") or guards
    assert inner.get("oof_class") == "complaint"
    assert inner.get("complaint_raised") is True
    assert inner.get("compose_fired") is True
    assert inner.get("escape_hatch_used") is not True


@pytest.mark.asyncio
async def test_oof_replay_hatch_under_5_percent(caplog):
    """Mocked few-shot replay of the live OOF gaps + table sketches.

    Live baseline was 3/17 ~18%. With compose selections, hatch must be <5%.
    """
    fixtures = [
        (
            "mausam kaisa hai?",
            [{"command": "compose", "fragments": ["irrelevant_redirect"], "oof_class": "irrelevant"}],
        ),
        (
            "yeh company bekar hai",
            [{"command": "compose", "fragments": ["ack_neutral", "fact_grievance"], "oof_class": "complaint"}],
        ),
        (
            "office kahan se?",
            [{"command": "compose", "fragments": ["fact_branch"], "oof_class": "call_context"}],
        ),
        (
            "branch kahan hai?",
            [{"command": "compose", "fragments": ["fact_branch"], "oof_class": "call_context"}],
        ),
        (
            "aaj ka match kaun jeeta",
            [{"command": "compose", "fragments": ["irrelevant_redirect"], "oof_class": "irrelevant"}],
        ),
        (
            "tumhari company fraud hai",
            [{"command": "compose", "fragments": ["ack_neutral", "fact_grievance"], "oof_class": "complaint"}],
        ),
        (
            "kaun si EMI?",
            [{"command": "start_flow", "flow": "plo_obj_which_emi"}],
        ),
        (
            "theek hai kar dunga",
            [{"command": "set_slot", "name": "plo_payment_intent", "value": "willing"}],
        ),
    ]
    hatch = 0
    oof_or_fact = 0
    for i, (text, cmds) in enumerate(fixtures):
        memory = InMemoryMemoryStore()
        call_id = f"w25-hatch-{i}"
        llm = _ScriptedLLM([cmds])
        with caplog.at_level(logging.INFO, logger="app.engine.turn_decision_log"):
            await handle_turn(
                _req(call_id, ""), memory=memory, llm=llm,
                tools=FakeToolClient(), kb=_EmptyKB(),
            )
            await handle_turn(
                _req(call_id, "haan, main Ramesh bol raha hoon"),
                memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
            )
            await handle_turn(
                _req(call_id, text),
                memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
            )
        guards = _guards(caplog, text[:12])
        inner = guards.get("guards") or guards
        oof_or_fact += 1
        if inner.get("escape_hatch_used"):
            hatch += 1
    rate = hatch / oof_or_fact
    assert rate < 0.05, f"hatch {hatch}/{oof_or_fact} = {rate:.0%} (want <5%)"


# ---------------------------------------------------------------------------
# W2-4b D1 cue-hit skip
# ---------------------------------------------------------------------------


def test_d1_identity_cue_hits_without_question():
    profile = get_tenant_profile(TENANT)
    assert profile is not None
    assert cue_hit_pack(
        "haan, main Ramesh bol raha hoon",
        "plo_identity_response",
        profile=profile,
        on_rails=True,
    ) == "identity"


def test_d1_question_shape_never_skips():
    profile = get_tenant_profile(TENANT)
    assert profile is not None
    assert cue_hit_pack(
        "haan. office kahan hai?",
        "plo_payment_intent",
        profile=profile,
        on_rails=True,
    ) is None


@pytest.mark.asyncio
async def test_d1_identity_turn_skips_llm():
    memory = InMemoryMemoryStore()
    call_id = "w25-d1"
    llm = _ScriptedLLM([[{"command": "clarify"}]])
    await handle_turn(
        _req(call_id, ""), memory=memory, llm=llm,
        tools=FakeToolClient(), kb=_EmptyKB(),
    )
    assert llm.call_count == 0  # opener skip
    await handle_turn(
        _req(call_id, "haan, main Ramesh bol raha hoon"),
        memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
    )
    assert llm.call_count == 0  # D1 identity cue-hit skip
    state = await memory.load_state(call_id)
    assert state is not None
    assert state.slots.get("plo_identity_response") in {"confirmed", "haan"}


# ---------------------------------------------------------------------------
# W2-4b D2 classification cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d2_repeat_weather_is_cache_hit(caplog):
    memory = InMemoryMemoryStore()
    call_id = "w25-d2"
    llm = _ScriptedLLM(
        [
            [
                {
                    "command": "compose",
                    "fragments": ["irrelevant_redirect"],
                    "oof_class": "irrelevant",
                }
            ]
        ]
    )
    with caplog.at_level(logging.INFO, logger="app.engine.turn_decision_log"):
        await handle_turn(
            _req(call_id, ""), memory=memory, llm=llm,
            tools=FakeToolClient(), kb=_EmptyKB(),
        )
        await handle_turn(
            _req(call_id, "haan, main Ramesh bol raha hoon"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
        await handle_turn(
            _req(call_id, "mausam kaisa hai?"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
        first_calls = llm.call_count
        await handle_turn(
            _req(call_id, "mausam kaisa hai?"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
    assert llm.call_count == first_calls  # second weather did not call LLM
    guards = _guards(caplog, "mausam")
    inner = guards.get("guards") or guards
    assert inner.get("class_cache_hit") is True
    assert int(inner.get("redirect_count") or 0) >= 1


# ---------------------------------------------------------------------------
# W2-4b D3 state-scoped catalog + A/B
# ---------------------------------------------------------------------------


def _predue_state():
    state = new_conversation_state("ab", TENANT, "b")
    state.slots["plo_scenario"] = "predue"
    state.flow_stack = [Frame(flow="plo_predue", step_index=1)]
    return state


def test_d3_predue_scope_keeps_which_emi_and_callback_drops_npa():
    profile = get_tenant_profile(TENANT)
    assert profile is not None
    flows = get_flow_set()
    full = tenant_flow_catalog(profile, flows)
    scoped = build_scoped_catalog(
        profile, flows, _predue_state(), "plo_payment_intent"
    )
    full_names = {c["name"] for c in full}
    scoped_names = {c["name"] for c in scoped}
    assert "plo_obj_which_emi" in scoped_names
    assert "plo_obj_callback_pd" in scoped_names
    assert "plo_predue" in scoped_names
    assert "plo_npa" not in scoped_names
    assert "plo_postdue1" not in scoped_names
    assert len(scoped) < len(full)
    assert "plo_npa" in full_names


def test_d3_scope_miss_accepts_in_full_catalog():
    raw = json.dumps([{"command": "start_flow", "flow": "plo_npa"}])
    result = parse_and_validate_commands(
        raw,
        candidate_flows=[{"name": "plo_predue", "description": "x"}],
        catalog_mode=True,
        full_catalog_names=frozenset({"plo_predue", "plo_npa"}),
    )
    assert result.scope_miss is True
    assert result.commands[0].flow == "plo_npa"


def test_d3_still_rejects_unknown_flow():
    raw = json.dumps([{"command": "start_flow", "flow": "not_a_real_flow"}])
    result = parse_and_validate_commands(
        raw,
        candidate_flows=[{"name": "plo_predue", "description": "x"}],
        catalog_mode=True,
        full_catalog_names=frozenset({"plo_predue"}),
    )
    assert result.scope_miss is False
    assert result.commands[0].command == "clarify"


def test_d3_ab_token_table():
    """Replay A/B: scoped catalog cuts tokens vs full; live-call flows in scope."""
    profile = get_tenant_profile(TENANT)
    assert profile is not None
    flows = get_flow_set()
    rows = []
    for scenario, slot in (
        ("predue", "plo_identity_response"),
        ("predue", "plo_payment_intent"),
        ("ondue", "plo_payment_intent"),
        ("postdue", "plo_payment_intent"),
        ("npa", "plo_timeline"),
    ):
        state = new_conversation_state(f"ab-{scenario}", TENANT, "b")
        state.slots["plo_scenario"] = scenario
        main = {
            "predue": "plo_predue",
            "ondue": "plo_ondue",
            "postdue": "plo_postdue1",
            "npa": "plo_npa",
        }[scenario]
        state.flow_stack = [Frame(flow=main, step_index=1)]
        full = tenant_flow_catalog(profile, flows)
        scoped = build_scoped_catalog(profile, flows, state, slot)
        rows.append(
            {
                "scenario": scenario,
                "slot": slot,
                "full_flows": len(full),
                "scoped_flows": len(scoped),
                "full_tokens": estimate_catalog_tokens(full),
                "scoped_tokens": estimate_catalog_tokens(scoped),
            }
        )
    # Every row must cut tokens; predue+intent must keep live-call flows.
    for row in rows:
        assert row["scoped_tokens"] < row["full_tokens"], row
        assert row["scoped_flows"] < row["full_flows"], row
    predue_intent = next(
        r for r in rows if r["scenario"] == "predue" and r["slot"] == "plo_payment_intent"
    )
    scoped = build_scoped_catalog(
        profile, flows, _predue_state(), "plo_payment_intent"
    )
    names = {c["name"] for c in scoped}
    # Live CALL A/B flows must not be out of scope (misroute / scope_miss = 0).
    assert "plo_obj_which_emi" in names
    assert "plo_obj_callback_pd" in names
    assert predue_intent["scoped_tokens"] > 0
