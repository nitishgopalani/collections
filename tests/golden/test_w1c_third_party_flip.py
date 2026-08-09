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
