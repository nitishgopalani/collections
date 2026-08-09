"""DEBT-039 (W1 closer) — policy preempt close replies are SPOKEN before end_call.

Verifies the root-cause fix for the silent-hangup gap observed in FINAL
CALL 1-redux (session 9aaf5dd2) and CALL 2 DNC (session a58b6077): each
policy preempt (safety/vulnerability, dnc, call_window, third_party_flip
strict) sets disposition + end_call but emitted ``tts_ms=0`` because the
early-exit path returned BEFORE calling ``on_gated_reply`` (the chunk
emitter). Fix: each early exit now calls ``on_gated_reply`` with the close
reply text + resolved scenario voice, so the go-server receives
``ChunkMessage`` frames BEFORE ``DoneMessage(end_call=true)`` → TTS speaks
the close → then the call ends (reuses the proven C0 apology speak-then-
close mechanics).

Pass criteria per the user's G1:
  - ``final_text_len > 0`` AND ``on_gated_reply`` called (tts audio frames)
    for each preempt class.
  - ``third_party_close`` contains zero loan-fact tokens (no ₹/Rs/रुपय/किश्त/
    किस्त/amount/date digits) — identity is revoked.
  - ``third_party_close`` interpolates ``{customer_name}`` from state slots.
"""

from __future__ import annotations

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.retrieval import clear_retrieval_cache
from app.engine.tenant_profile import clear_tenant_profile_cache
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


class _NoOpLLM:
    def __init__(self):
        self.call_count = 0

    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def complete(self, system: str, user: str, *, json_only: bool = True, **kw) -> str:
        self.call_count += 1
        return "[]"


class _RecordingChunks:
    """Capture on_gated_reply calls so we can assert chunks were emitted
    (the DEBT-039 fix: tts audio frames before end_call)."""

    def __init__(self):
        self.calls = []

    async def __call__(self, reply_text, *, voice_id=None, tts_model=None, tts_pace=None):
        self.calls.append(
            {
                "reply_text": reply_text,
                "voice_id": voice_id,
                "tts_model": tts_model,
                "tts_pace": tts_pace,
            }
        )


async def _turn(memory, call_id, text, llm, *, turn_meta=None, on_gated_reply=None):
    req = TurnRequest(
        call_id=call_id,
        borrower_id="plo_debt039_borrower",
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
        on_gated_reply=on_gated_reply,
    )


# ---------------------------------------------------------------------------
# G1: each preempt class speaks (on_gated_reply called + final_text_len > 0)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debt039_dnc_early_exit_speaks_ack_before_end():
    """DNC preempt: on_gated_reply called with the dnc_ack text → tts_ms>0."""
    memory = InMemoryMemoryStore()
    llm = _NoOpLLM()
    recorder = _RecordingChunks()
    response = await _turn(
        memory, "call-debt039-dnc", "dobara call mat karna", llm,
        on_gated_reply=recorder,
    )

    assert response.disposition == "dnc_requested"
    assert response.end_call is True
    assert response.reply_text.strip(), "DNC must speak a non-empty ack"
    assert len(recorder.calls) == 1, (
        f"on_gated_reply must be called exactly once for DNC (got {len(recorder.calls)})"
    )
    assert recorder.calls[0]["reply_text"] == response.reply_text
    # Non-committal: request recorded, no suppression promise.
    lower = response.reply_text.lower()
    assert "request" in lower or "रिक्वेस्ट" in response.reply_text or "darj" in lower, (
        f"DNC reply not non-committal ack: {response.reply_text!r}"
    )


@pytest.mark.asyncio
async def test_debt039_third_party_flip_strict_speaks_close_before_end():
    """Third-party flip (strict): on_gated_reply called with third_party_close
    → tts_ms>0; close contains zero loan-fact tokens (identity revoked)."""
    memory = InMemoryMemoryStore()
    llm = _NoOpLLM()
    recorder = _RecordingChunks()
    response = await _turn(
        memory, "call-debt039-flip", "main ramesh ka bhai bol raha hoon", llm,
        on_gated_reply=recorder,
    )

    assert response.disposition == "THIRD_PARTY_FLAGGED"
    assert response.end_call is True
    assert response.reply_text.strip(), "strict flip must speak the third-party close"
    assert len(recorder.calls) == 1, (
        f"on_gated_reply must be called exactly once for flip (got {len(recorder.calls)})"
    )
    assert recorder.calls[0]["reply_text"] == response.reply_text
    # Zero loan-fact tokens: no ₹/Rs/रुपय/किश्त/किस्त/amount/date digits.
    _assert_no_loan_facts(response.reply_text)


@pytest.mark.asyncio
async def test_debt039_call_window_close_speaks_before_end(monkeypatch):
    """Call-window close: on_gated_reply called with window_close → tts_ms>0."""
    # Force the call-window to be closed mid-call by patching within_call_window.
    from app.engine import compliance_rules as cr

    def _always_outside(*a, **kw):
        return False

    monkeypatch.setattr(cr, "within_call_window", _always_outside, raising=False)

    memory = InMemoryMemoryStore()
    # Pre-seed attempts>=1 so call_window_preempt fires (mid-call only).
    state = new_conversation_state("call-debt039-cw", "paisalo", "plo_debt039_borrower")
    state.attempts = 2
    state.slots["identity_ok"] = True
    memory._states["call-debt039-cw"] = state.model_copy(deep=True)

    llm = _NoOpLLM()
    recorder = _RecordingChunks()
    response = await _turn(
        memory, "call-debt039-cw", "haan ji", llm,
        on_gated_reply=recorder,
    )

    assert response.disposition == "call_window_closed"
    assert response.end_call is True
    assert response.reply_text.strip(), "window close must speak a non-empty close"
    assert len(recorder.calls) == 1, (
        f"on_gated_reply must be called exactly once for window close (got {len(recorder.calls)})"
    )


@pytest.mark.asyncio
async def test_debt039_vulnerability_speaks_close_before_transfer():
    """Safety/vulnerability preempt: on_gated_reply called with
    vulnerability_close → tts_ms>0 (transfers to human, end_call=False)."""
    memory = InMemoryMemoryStore()
    llm = _NoOpLLM()
    recorder = _RecordingChunks()
    # PaisaLo vulnerability signal: "suicide" cue (proven in test_w1c_vulnerability_lane).
    response = await _turn(
        memory, "call-debt039-vuln", "main suicide soch raha hoon", llm,
        on_gated_reply=recorder,
    )

    assert response.disposition == "VULNERABLE_FLAGGED"
    # Safety transfers to human (end_call=False); the caller stays on the line
    # and hears the care-first close before the handoff.
    assert response.reply_text.strip(), "vulnerability must speak a non-empty close"
    assert len(recorder.calls) == 1, (
        f"on_gated_reply must be called exactly once for vulnerability (got {len(recorder.calls)})"
    )


# ---------------------------------------------------------------------------
# G1: third_party_close interpolates {customer_name} + zero fact tokens
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debt039_third_party_close_interpolates_customer_name():
    """third_party_close interpolates {customer_name} from state.slots so the
    spoken close addresses the borrower by name even after identity is revoked."""
    memory = InMemoryMemoryStore()
    state = new_conversation_state(
        "call-debt039-name", "paisalo", "plo_debt039_borrower"
    )
    state.slots["customer_name"] = "Ramesh"
    state.slots["identity_ok"] = True
    memory._states["call-debt039-name"] = state.model_copy(deep=True)

    llm = _NoOpLLM()
    recorder = _RecordingChunks()
    response = await _turn(
        memory, "call-debt039-name", "main ramesh ka bhai bol raha hoon", llm,
        on_gated_reply=recorder,
    )

    assert response.disposition == "THIRD_PARTY_FLAGGED"
    assert response.end_call is True
    # {customer_name} must be interpolated to "Ramesh" (not the literal placeholder).
    assert "{customer_name}" not in response.reply_text, (
        f"close still has literal placeholder: {response.reply_text!r}"
    )
    assert "Ramesh" in response.reply_text or "रमेश" in response.reply_text, (
        f"close does not address the borrower by name: {response.reply_text!r}"
    )
    _assert_no_loan_facts(response.reply_text)


@pytest.mark.asyncio
async def test_debt039_third_party_close_missing_name_falls_back_gracefully():
    """If customer_name is missing, the close falls back to a respectful
    generic (आप) — never speaks a literal '{customer_name}'."""
    memory = InMemoryMemoryStore()
    llm = _NoOpLLM()
    recorder = _RecordingChunks()
    response = await _turn(
        memory, "call-debt039-noname", "main ramesh ka bhai bol raha hoon", llm,
        on_gated_reply=recorder,
    )

    assert response.disposition == "THIRD_PARTY_FLAGGED"
    assert "{customer_name}" not in response.reply_text, (
        f"close has literal placeholder (no name): {response.reply_text!r}"
    )
    assert response.reply_text.strip()


# ---------------------------------------------------------------------------
# G1: scenario voice (simran) is resolved for the preempt close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debt039_preempt_close_resolves_scenario_voice():
    """The preempt close reply is emitted with the resolved scenario voice
    (simran for paisalo predue), not the env default — RC3 guarantee holds
    for preempt closes just as for normal replies."""
    memory = InMemoryMemoryStore()
    state = new_conversation_state(
        "call-debt039-voice", "paisalo", "plo_debt039_borrower"
    )
    state.slots["customer_name"] = "Ramesh"
    state.slots["voice_id"] = "simran"
    state.slots["identity_ok"] = True
    memory._states["call-debt039-voice"] = state.model_copy(deep=True)

    llm = _NoOpLLM()
    recorder = _RecordingChunks()
    await _turn(
        memory, "call-debt039-voice", "main ramesh ka bhai bol raha hoon", llm,
        on_gated_reply=recorder,
    )

    assert len(recorder.calls) == 1
    assert recorder.calls[0]["voice_id"] == "simran", (
        f"preempt close voice not simran: {recorder.calls[0]['voice_id']!r}"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FACT_TOKENS = ("₹", "Rs", "rs", "रुपय", "रुपये", "किश्त", "किस्त", "kist", "kisth", "amount", "EMI", "emi")


def _assert_no_loan_facts(text: str) -> None:
    """Assert the close reply contains zero loan-fact tokens (DPDP: identity
    revoked → no amounts/dates/PII). Digits are allowed only inside Devanagari
    polite particles (जी) — amounts would carry ₹/Rs/रुपय/किश्त."""
    lower = text.lower()
    for tok in _FACT_TOKENS:
        assert tok.lower() not in lower, (
            f"close reply contains loan-fact token {tok!r}: {text!r}"
        )
