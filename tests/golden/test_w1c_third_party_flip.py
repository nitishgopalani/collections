"""W1-C C4 — third-party / speaker-flip guard (policy interrupt) + DPDP amendment.

Verifies the policy-lane speaker-flip detector fires BEFORE the Tier-1
evidence scorer on mid-call cues (``main uski/uska X bol raha/rahi``,
``wo bahar hai, main…``, ``main ramesh ka bhai bol raha hoon`` …), revokes
``identity_current``, locks disclosure (strict) or downgrades to generic-only
(relaxed), speaks the third-party script + callback capture, and tags
``disposition=THIRD_PARTY_FLAGGED``.

DPDP posture is BRAND-CONFIGURABLE (W1-C amendment):
  strict  = disclosure LOCK → third-party script → callback → END (outcome 7).
  relaxed = generic-only facts (no amounts/dates/PII); conversation may continue.
  open_tier = log suspicion only; no lock, no end (lab use).
ALWAYS-ON regardless of mode: third_party_suspected=true + identity_current
transition logged + disposition tagged.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings, tenant_config
from app.engine.retrieval import clear_retrieval_cache
from app.engine.safety import third_party_flip_preempt
from app.engine.tenant_profile import TenantRuntimeProfile, clear_tenant_profile_cache
from app.engine.tracker import new_conversation_state
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

    async def complete(self, system: str, user: str, *, json_only: bool = True, **kw) -> str:
        self.call_count += 1
        if self.call_count <= len(self._responses):
            return self._responses[self.call_count - 1]
        return "[]"


async def _turn(memory, call_id, text, llm, *, turn_meta=None):
    req = TurnRequest(
        call_id=call_id,
        borrower_id="plo_flip_borrower",
        tenant_id="paisalo",
        channel="voice",
        locale="hi-IN",
        transcript=text,
        turn_meta=turn_meta or {"force_flow": "plo_opener", "call_date": CALL_DATE},
    )
    return await handle_turn(
        req,
        memory=memory,
        llm=llm,
        tools=FakeToolClient(),
        kb=_EmptyKB(),
    )


def _profile(**overrides):
    base = dict(
        tenant_id="paisalo",
        flow_prefix="plo_",
        dpdp_third_party_lock="strict",
        dpdp_disclosure_tier_enforced=True,
    )
    base.update(overrides)
    return TenantRuntimeProfile(**base)


@pytest.mark.parametrize(
    "cue",
    [
        "main ramesh ka bhai bol raha hoon",
        "main uski taraf se bol raha hoon",
        "wo bahar hai, main hi baat kar raha hoon",
        "main uska bhai hoon",
        "main uski beti bol rahi hoon",
        "he is not here, i am his brother",
    ],
)
@pytest.mark.asyncio
async def test_c4_strict_flip_preempts_revokes_identity_locks_disclosure_ends(cue):
    """Strict mode (paisalo default): flip cue → identity revoked, disclosure LOCK,
    third-party script + callback, END, disposition=THIRD_PARTY_FLAGGED."""
    memory = InMemoryMemoryStore()
    llm = _ScriptedLLM([[{"command": "start_flow", "flow": "plo_opener"}]])
    response = await _turn(memory, "call-flip-strict", cue, llm)

    # Policy-lane preemption: scorer never ran.
    assert llm.call_count == 0, "scorer must not run on a speaker-flip cue"
    # Named disposition + graceful END (outcome 7, strict).
    assert response.disposition == "THIRD_PARTY_FLAGGED"
    assert response.end_call is True
    # Third-party script + callback capture spoken (non-empty, no loan facts).
    assert response.reply_text.strip()
    assert "borrower" in response.reply_text.lower() or "ramesh" in response.reply_text.lower()
    # No flow was started.
    assert response.actions_executed == []


def test_c4_strict_unit_revokes_identity_and_locks_disclosure():
    cfg = tenant_config("paisalo")
    state = new_conversation_state("c-flip-u", "paisalo", "plo_flip_borrower")
    state.slots["identity_ok"] = True  # identity was current before the flip
    result = third_party_flip_preempt(
        "main ramesh ka bhai bol raha hoon", state, cfg, profile=_profile()
    )
    assert result is not None
    assert result.end_call is True
    assert result.compliance_updates.get("third_party_suspected") is True
    assert result.compliance_updates.get("third_party_active") is True
    assert result.compliance_updates.get("identity_ok") is False
    assert result.compliance_updates.get("dpdp_mode") == "strict"


def test_c4_relaxed_unit_no_end_but_still_locks_and_tags():
    cfg = tenant_config("paisalo")
    state = new_conversation_state("c-flip-rel", "paisalo", "plo_flip_borrower")
    state.slots["identity_ok"] = True
    result = third_party_flip_preempt(
        "main uski taraf se bol raha hoon",
        state,
        cfg,
        profile=_profile(dpdp_third_party_lock="relaxed"),
    )
    assert result is not None
    assert result.end_call is False  # relaxed: conversation may continue
    # ALWAYS-ON: identity revoked + disclosure LOCK + disposition tagged.
    assert result.compliance_updates.get("third_party_suspected") is True
    assert result.compliance_updates.get("third_party_active") is True
    assert result.compliance_updates.get("identity_ok") is False
    assert result.compliance_updates.get("dpdp_mode") == "relaxed"


def test_c4_open_tier_unit_logs_suspicion_without_lock_or_end():
    cfg = tenant_config("paisalo")
    state = new_conversation_state("c-flip-open", "paisalo", "plo_flip_borrower")
    state.slots["identity_ok"] = True
    result = third_party_flip_preempt(
        "main ramesh ka bhai bol raha hoon",
        state,
        cfg,
        profile=_profile(dpdp_disclosure_tier_enforced=False),
    )
    assert result is not None
    assert result.end_call is False
    # Open-tier: suspicion logged but NO disclosure lock, NO identity revoke.
    assert result.compliance_updates.get("third_party_suspected") is True
    assert result.compliance_updates.get("third_party_open_tier") is True
    assert not result.compliance_updates.get("third_party_active")
    assert not result.compliance_updates.get("identity_ok") is False  # identity NOT revoked


@pytest.mark.asyncio
async def test_c4_relaxed_mode_logs_audit_lines_and_continues(monkeypatch, caplog):
    """Relaxed mode: conversation continues (end_call=False) AND the
    always-on audit log-lines are present (third_party_suspected=true +
    identity_current transition)."""
    import app.engine.turn as turn_mod

    # Monkeypatch the profile resolver to return a relaxed-mode profile.
    _relaxed_profile = _profile(dpdp_third_party_lock="relaxed")

    def _fake_profile(tenant_id):
        return _relaxed_profile

    # clear_tenant_profile_cache() (called in fixture teardown) calls
    # get_tenant_profile.cache_clear(); expose a no-op so teardown is clean.
    _fake_profile.cache_clear = lambda: None  # type: ignore[attr-defined]

    monkeypatch.setattr(turn_mod, "get_tenant_profile", _fake_profile, raising=False)
    # Also patch the safety import path used by third_party_flip_check_transcript.
    import app.engine.tenant_profile as tp_mod

    monkeypatch.setattr(tp_mod, "get_tenant_profile", _fake_profile, raising=False)

    memory = InMemoryMemoryStore()
    llm = _ScriptedLLM([[{"command": "start_flow", "flow": "plo_opener"}]])
    caplog.set_level(logging.INFO, logger="app.engine.turn")
    response = await _turn(memory, "call-flip-relaxed", "main ramesh ka bhai bol raha hoon", llm)

    assert response.disposition == "THIRD_PARTY_FLAGGED"
    assert response.end_call is False  # relaxed: continues
    # Audit log-lines present regardless of mode (ALWAYS-ON).
    msgs = [r.getMessage() for r in caplog.records]
    assert any("third_party_suspected=true" in m for m in msgs), msgs
    assert any("identity_current transition" in m for m in msgs), msgs


@pytest.mark.asyncio
async def test_c4_disclosure_never_continues_after_flip_strict():
    """After a strict flip, a subsequent turn that would disclose loan facts
    is blocked by the disclosure LOCK (third_party_active → must_block_debt_disclosure
    → slots_for_nlg strips DEBT_SLOT_KEYS). The gate blocks debt disclosure."""
    from app.engine.identity_gate import must_block_debt_disclosure

    cfg = tenant_config("paisalo")
    state = new_conversation_state("c-flip-lock", "paisalo", "plo_flip_borrower")
    state.slots["identity_ok"] = True
    # Simulate the flip having fired: identity revoked + third_party_active.
    result = third_party_flip_preempt(
        "main ramesh ka bhai bol raha hoon", state, cfg, profile=_profile()
    )
    from app.engine.safety import apply_third_party_flip_to_state

    state = apply_third_party_flip_to_state(state, result)
    # Disclosure LOCK is now active — debt disclosure must be blocked.
    assert must_block_debt_disclosure(state.slots) is True
    assert state.slots.get("identity_ok") is False
    assert state.slots.get("third_party_active") is True


# ---------------------------------------------------------------------------
# DEBT-030: policy-lane classification on superseded / terminal-race turns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debt030_third_party_cue_on_terminal_race_turn_classifies_flagged():
    """A barge-final third-party cue on a turn that is already ending (prior
    turn set end_call=True via repair escalation) must STILL classify as
    THIRD_PARTY_FLAGGED, not fall through to the terminal guard's prior
    ESCALATED_UNCLEAR.

    Regression for live CALL 1 Session A t7 (DEBT-030): ASR captured
    "uska bhai bol raha hoon" but the turn hit the terminal guard BEFORE the
    preempts (end_call=True from t5 repair escalation) and re-issued the
    prior ESCALATED_UNCLEAR. Fix: preempts run BEFORE the terminal guard so a
    third-party / DNC / vulnerability cue in the final transcript classifies
    truthfully even when the call is already ending.
    """
    from app.engine.tracker import new_conversation_state

    memory = InMemoryMemoryStore()
    call_id = "call-debt030-flip"

    # Pre-seed a terminal-race state: a prior turn already set end_call +
    # sot_call_closed + disposition=ESCALATED_UNCLEAR (the repair-escalation
    # outcome). The barge-in turn loads this state.
    state = new_conversation_state(call_id, "paisalo", "plo_flip_borrower")
    state.version = 1
    state.slots["end_call"] = True
    state.slots["sot_call_closed"] = True
    state.slots["disposition"] = "ESCALATED_UNCLEAR"
    state.slots["identity_ok"] = True
    state.flow_stack = []  # empty (call already closing)
    memory._states[call_id] = state.model_copy(deep=True)

    llm = _ScriptedLLM([[{"command": "start_flow", "flow": "plo_opener"}]])
    response = await _turn(memory, call_id, "uska bhai bol raha hoon", llm)

    # DEBT-030 fix: preempts run BEFORE the terminal guard, so the third-party
    # cue classifies the turn as THIRD_PARTY_FLAGGED (truthful) instead of the
    # terminal guard re-issuing the prior ESCALATED_UNCLEAR.
    assert response.disposition == "THIRD_PARTY_FLAGGED", response.disposition
    assert response.end_call is True
    assert response.reply_text.strip(), "strict flip must speak the third-party close"


@pytest.mark.asyncio
async def test_debt030_third_party_cue_devanagari_on_terminal_race_classifies_flagged():
    """Devanagari bare 'uska bhai' (उसका भाई) on a terminal-race turn must
    classify as THIRD_PARTY_FLAGGED. Covers the live ASR transcript form
    'मैं तो रमेश बोल ही नहीं रहा हूँ। उसका भाई बोल रहा हूँ।' (Session A t7)."""
    from app.engine.tracker import new_conversation_state

    memory = InMemoryMemoryStore()
    call_id = "call-debt030-flip-deva"
    state = new_conversation_state(call_id, "paisalo", "plo_flip_borrower")
    state.version = 1
    state.slots["end_call"] = True
    state.slots["sot_call_closed"] = True
    state.slots["disposition"] = "ESCALATED_UNCLEAR"
    state.slots["identity_ok"] = True
    state.flow_stack = []
    memory._states[call_id] = state.model_copy(deep=True)

    llm = _ScriptedLLM([[{"command": "start_flow", "flow": "plo_opener"}]])
    response = await _turn(
        memory, call_id, "मैं तो रमेश बोल ही नहीं रहा हूँ। उसका भाई बोल रहा हूँ।", llm
    )
    assert response.disposition == "THIRD_PARTY_FLAGGED", response.disposition
    assert response.end_call is True


@pytest.mark.asyncio
async def test_debt030_no_cue_on_terminal_race_still_early_exits():
    """A late barge-in with NO policy cue after end_call=True must still hit
    the terminal guard (silent re-issue end_call). Ensures the DEBT-030
    reorder didn't break the existing terminal-guard behaviour for non-cue
    barge-ins."""
    from app.engine.tracker import new_conversation_state

    memory = InMemoryMemoryStore()
    call_id = "call-debt030-nocue"
    state = new_conversation_state(call_id, "paisalo", "plo_flip_borrower")
    state.version = 1
    state.slots["end_call"] = True
    state.slots["sot_call_closed"] = True
    state.slots["disposition"] = "ESCALATED_UNCLEAR"
    state.flow_stack = []
    memory._states[call_id] = state.model_copy(deep=True)

    llm = _ScriptedLLM([[{"command": "clarify"}]])
    response = await _turn(memory, call_id, "ok bye", llm)
    # No cue → preempts fall through → terminal guard re-issues end_call.
    assert response.end_call is True
    assert response.reply_text == ""
    assert response.reply_id is None


# Y1 (DEBT-032): name-agnostic relation substrings — the exact live ASR
# form "main Ramesh ka bhai bol raha hoon" must fire the preempt via the
# "ka bhai" substring, without enumerating borrower names.
@pytest.mark.parametrize(
    "cue",
    [
        "main ramesh ka bhai bol raha hoon",
        "accha suno main ramesh ka bhai bol raha hoon wo bahar gaya hai",
        "main ramesh ki behen bol rahi hoon",
        "main ramesh ka beta hoon",
        "main ramesh ki patni bol rahi hoon",
        "main ramesh ka dost hoon",
        "main ramesh ke papa bol rahe hain",
        "main ramesh ki mummy hoon",
        # Devanagari named forms
        "मैं रमेश का भाई बोल रहा हूँ",
        "अच्छा सुनो मैं रमेश का भाई बोल रहा हूँ वो बाहर गया है",
        "मैं रमेश की बहन बोल रही हूँ",
        "मैं रमेश का दोस्त हूँ",
    ],
)
def test_debt032_named_relation_substring_fires_flip(cue):
    """Y1: name-agnostic relation substrings (ka bhai / का भाई / ki behen
    / ka beta / ki patni / ka dost / ke papa / ki mummy) fire the
    third_party_flip_preempt on the exact live ASR form
    "main Ramesh ka bhai bol raha hoon" without enumerating names."""
    from app.config import tenant_config

    tenant_cfg = tenant_config("paisalo")
    state = new_conversation_state("call-debt032-unit", "paisalo", "plo_flip_borrower")
    result = third_party_flip_preempt(cue, state, tenant_cfg, profile=_profile())
    assert result is not None, f"expected flip to fire for: {cue!r}"
    assert "third_party_flip" in result.reason


def test_debt032_mera_bhai_does_not_fire():
    """Y1 guard against over-broad match: "mera bhai" (caller's own brother)
    must NOT fire — "ka bhai" requires the "ka " (genitive) immediately
    before "bhai", which "mera bhai" does not contain."""
    from app.config import tenant_config

    tenant_cfg = tenant_config("paisalo")
    state = new_conversation_state("call-debt032-neg", "paisalo", "plo_flip_borrower")
    result = third_party_flip_preempt("mera bhai bahar hai", state, tenant_cfg, profile=_profile())
    assert result is None
