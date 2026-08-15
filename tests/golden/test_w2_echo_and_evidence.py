"""W2-1 — Echo filter + Evidence scorer (telemetry-only).

Echo filter (app/engine/echo_filter.py): fuzzy match transcript vs the bot's
last spoken reply -> drop turn, echo_suspected=true, outcome=HOLD, zero
counter burn. Runs BEFORE policy preempts so the bot's own spoken legal lines
cannot self-trigger the policy lane.

Evidence scorer (app/engine/evidence_scorer.py): 0-3 score logged per turn in
the turn_decision guards dict. TELEMETRY-ONLY this phase (the Commitment Gate
consumes it in W2-2).

Fixtures are drawn from real PREDUE call transcripts:
  - 660acb01 t2: identity confirm (score 3)
  - willing cue "theek hai kar dunga" at plo_payment_intent (score 2)
  - backchannel "hmm" / "achha" (score 0)
  - blank transcript (score 0, non-addressed)
"""

from __future__ import annotations

import json
import logging

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.echo_filter import detect_echo, echo_match_threshold
from app.engine.evidence_scorer import score_evidence
from app.engine.retrieval import clear_retrieval_cache
from app.engine.tenant_profile import clear_tenant_profile_cache, get_tenant_profile
from app.engine.tracker import new_conversation_state
from app.engine.turn import handle_turn
from app.flows.loader import reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest

CALL_DATE = "2026-08-10"


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


class _CountingLLM:
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


def _req(call_id: str, text: str, borrower_id: str = "plo_w21_borrower") -> TurnRequest:
    return TurnRequest(
        call_id=call_id,
        borrower_id=borrower_id,
        tenant_id="paisalo",
        channel="voice",
        locale="hi-IN",
        transcript=text,
        turn_meta={"force_flow": "plo_opener", "call_date": CALL_DATE},
    )


class TestEchoFilter:
    def test_exact_normalized_match_is_echo(self):
        line = "नमस्ते, मैं पैसालो से बोल रहा हूँ। क्या मेरी बात रमेश जी से हो रही है?"
        assert detect_echo(line, line) is True

    def test_nukta_insensitive_match(self):
        assert detect_echo("सख़्त कार्रवाई", "सख्त कार्रवाई") is True

    def test_high_jaccard_overlap_is_echo(self):
        reply = "ठीक है, हम बाद में रमेश जी से संपर्क कर लेंगे। धन्यवाद।"
        echo = "ठीक है, हम बाद में रमेश जी से संपर्क कर लेंगे।"
        assert detect_echo(echo, reply) is True

    def test_genuine_answer_is_not_echo(self):
        reply = "क्या मेरी बात रमेश जी से हो रही है?"
        answer = "हाँ, मैं रमेश बोल रहा हूँ।"
        assert detect_echo(answer, reply) is False

    def test_bare_yes_is_not_echo(self):
        assert detect_echo("haan", "क्या मेरी बात रमेश जी से हो रही है?") is False
        assert detect_echo("haan", "") is False
        assert detect_echo("", "anything") is False

    def test_short_substring_of_reply_is_echo(self):
        reply = "ठीक है, आपकी यह रिक्वेस्ट दर्ज हो गई है। धन्यवाद।"
        echo = "दर्ज हो गई है"
        assert detect_echo(echo, reply) is True

    def test_threshold_env_config(self, monkeypatch):
        monkeypatch.setenv("ECHO_MATCH_THRESHOLD", "0.9")
        assert echo_match_threshold() == 0.9
        monkeypatch.setenv("ECHO_MATCH_THRESHOLD", "bogus")
        assert echo_match_threshold() == 0.7


def _profile():
    return get_tenant_profile("paisalo")


def _state_with_last(transcript: str | None):
    state = new_conversation_state("w21-unit", "paisalo", "plo_w21_borrower")
    if transcript is not None:
        state.slots["_last_borrower_transcript"] = transcript
    return state


class TestEvidenceScorer:
    def test_score3_explicit_confirm_identity(self):
        score = score_evidence(
            transcript="हाँ, मैं रमेश बोल रहा हूँ।",
            state=_state_with_last(None),
            profile=_profile(),
            llm_calls=1, commands=[], last_spoken_reply="",
            echo=False, awaited_slot="plo_identity_response",
        )
        assert score["evidence"] == 3
        assert score["evidence_reason"] == "explicit_confirm"

    def test_score3_bare_haan_at_confirm_slot(self):
        score = score_evidence(
            transcript="haan",
            state=_state_with_last(None),
            profile=_profile(),
            llm_calls=1, commands=[], last_spoken_reply="",
            echo=False, awaited_slot="sot_final_confirm",
        )
        assert score["evidence"] == 3

    def test_score2_cue_agree_willing(self):
        score = score_evidence(
            transcript="theek hai kar dunga",
            state=_state_with_last(None),
            profile=_profile(),
            llm_calls=1, commands=[], last_spoken_reply="",
            echo=False, awaited_slot="plo_payment_intent",
        )
        assert score["evidence"] == 2
        assert score["evidence_reason"] == "cue_agree"

    def test_score3_haan_pakka_with_pending_confirm(self):
        """W2-4 enforce: when the gate issued a confirm-ask last turn
        (_pending_confirm set), a bare yes-token at a COLLECT slot scores
        3 (explicit_confirm), not 2 (cue_agree). Live call cf7d4e08 showed
        "haan pakka" scoring 2 at plo_payment_intent → gate kept
        downgrading → repair escalated. Fix: pending_confirm relaxes the
        confirm-slot marker check in _explicit_confirm.
        """
        score = score_evidence(
            transcript="haan pakka",
            state=_state_with_last(None),
            profile=_profile(),
            llm_calls=1, commands=[], last_spoken_reply="",
            echo=False, awaited_slot="plo_payment_intent",
            pending_confirm=True,
        )
        assert score["evidence"] == 3
        assert score["evidence_reason"] == "explicit_confirm"
        assert score["evidence_signals"].get("pending_confirm") is True

    def test_score2_haan_pakka_without_pending_confirm(self):
        """Without a pending confirm, "haan pakka" at a collect slot is
        still cue_agree (evidence 2) — the pending_confirm flag is what
        promotes it to explicit_confirm."""
        score = score_evidence(
            transcript="haan pakka",
            state=_state_with_last(None),
            profile=_profile(),
            llm_calls=1, commands=[], last_spoken_reply="",
            echo=False, awaited_slot="plo_payment_intent",
            pending_confirm=False,
        )
        assert score["evidence"] == 2

    def test_e3_haan_office_question_not_explicit_confirm(self):
        """E3: pending_confirm + yes-token + question-markers is NOT
        explicit_confirm. Live dc4c5808 t4 "हाँ। ऑफिस कहाँ है?" scored 3
        and committed a phantom willing."""
        from app.engine.evidence_scorer import has_question_shape

        assert has_question_shape("हाँ। ऑफिस कहाँ है?") is True
        score = score_evidence(
            transcript="हाँ। ऑफिस कहाँ है?",
            state=_state_with_last(None),
            profile=_profile(),
            llm_calls=1, commands=[], last_spoken_reply="",
            echo=False, awaited_slot="plo_payment_intent",
            pending_confirm=True,
        )
        assert score["evidence"] != 3
        assert score["evidence_reason"] != "explicit_confirm"

    def test_score2_borrower_repeated(self):
        # A repeat with NO cue-pack words so cue_agree doesn't preempt the
        # borrower_repeated reason. "office mein meeting chal rahi hai" has
        # no negation / yes / willing tokens.
        repeated_text = "office mein meeting chal rahi hai"
        state = _state_with_last(repeated_text)
        score = score_evidence(
            transcript=repeated_text,
            state=state, profile=_profile(),
            llm_calls=1, commands=[], last_spoken_reply="",
            echo=False, awaited_slot="plo_payment_intent",
        )
        assert score["evidence"] == 2
        assert score["evidence_reason"] == "borrower_repeated"

    def test_score1_llm_only(self):
        state = _state_with_last("kuch aur")
        score = score_evidence(
            transcript="mera phone number change ho gaya hai",
            state=state, profile=_profile(),
            llm_calls=1, commands=[], last_spoken_reply="",
            echo=False, awaited_slot="plo_payment_intent",
        )
        assert score["evidence"] == 1
        assert score["evidence_reason"] == "llm_only"

    def test_score0_backchannel(self):
        score = score_evidence(
            transcript="hmm",
            state=_state_with_last(None), profile=_profile(),
            llm_calls=0, commands=[], last_spoken_reply="",
            echo=False, awaited_slot="plo_payment_intent",
        )
        assert score["evidence"] == 0
        assert score["evidence_reason"] == "backchannel"

    def test_score0_backchannel_devanagari(self):
        score = score_evidence(
            transcript="अच्छा अच्छा",
            state=_state_with_last(None), profile=_profile(),
            llm_calls=0, commands=[], last_spoken_reply="",
            echo=False, awaited_slot="plo_payment_intent",
        )
        assert score["evidence"] == 0
        assert score["evidence_reason"] == "backchannel"

    def test_score0_non_addressed_blank(self):
        score = score_evidence(
            transcript="",
            state=_state_with_last(None), profile=_profile(),
            llm_calls=0, commands=[], last_spoken_reply="",
            echo=False, awaited_slot="plo_payment_intent",
        )
        assert score["evidence"] == 0
        assert score["evidence_reason"] == "non_addressed_blank"

    def test_score0_non_addressed_scripted_no_cue(self):
        score = score_evidence(
            transcript="mera phone number change ho gaya hai",
            state=_state_with_last(None), profile=_profile(),
            llm_calls=0, commands=[], last_spoken_reply="",
            echo=False, awaited_slot="plo_payment_intent",
        )
        assert score["evidence"] == 0
        assert score["evidence_reason"] == "non_addressed"

    def test_score0_echo_wins_over_backchannel(self):
        score = score_evidence(
            transcript="ठीक है",
            state=_state_with_last(None), profile=_profile(),
            llm_calls=0, commands=[],
            last_spoken_reply="ठीक है, हम बाद में रमेश जी से संपर्क कर लेंगे।",
            echo=True, awaited_slot="plo_payment_intent",
        )
        assert score["evidence"] == 0
        assert score["evidence_reason"] == "echo"


class TestEchoHoldIntegration:
    @pytest.mark.asyncio
    async def test_echo_hold_drops_turn_zero_counter_burn(self, caplog):
        memory = InMemoryMemoryStore()
        call_id = "w21-echo"
        llm = _CountingLLM()

        with caplog.at_level(logging.INFO, logger="app.engine.turn_decision_log"):
            t1 = await handle_turn(
                _req(call_id, ""), memory=memory, llm=llm,
                tools=FakeToolClient(), kb=_EmptyKB(),
            )
        assert t1.reply_text
        opener_reply = t1.reply_text
        assert llm.call_count == 0  # opener LLM skip

        llm2 = _CountingLLM()
        with caplog.at_level(logging.INFO, logger="app.engine.turn_decision_log"):
            t2 = await handle_turn(
                _req(call_id, opener_reply), memory=memory, llm=llm2,
                tools=FakeToolClient(), kb=_EmptyKB(),
            )
        assert t2.disposition == "ECHO_HOLD"
        assert t2.end_call is False
        assert t2.reply_text == ""
        assert llm2.call_count == 0  # zero counter burn — no LLM

        echo_lines = [
            r.getMessage()
            for r in caplog.records
            if "turn_decision" in r.getMessage() and opener_reply[:20] in r.getMessage()
        ]
        assert echo_lines, "echo turn_decision log not emitted"
        assert '"echo_suspected": true' in echo_lines[-1]
        assert '"evidence": 0' in echo_lines[-1]
        assert '"outcome": "HOLD"' in echo_lines[-1]
