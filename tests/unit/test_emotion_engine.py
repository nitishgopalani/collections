"""Sprint 11 — Emotion Engine tests."""

from datetime import UTC, datetime

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import tenant_config
from app.engine.gate import gate
from app.engine.nlg import render
from app.engine.safety import safety_preempt
from app.engine.tracker import hydrate_from_borrower, new_conversation_state
from app.engine.turn import handle_turn
from app.engines_p2.emotion import (
    EMOTION_IS_INPUT_NOT_LICENSE,
    apply_emotion_to_state,
    classify_emotion_from_turn,
    classify_emotion_llm,
    classify_emotion_rules,
    emotion_triggers_safety,
    parse_and_validate_emotion,
    select_tone_register,
    sync_emotion_on_persist,
)
from app.flows.loader import load_all_flows
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.schemas.command import Command
from app.schemas.state import BorrowerRecord
from tests.fixtures.test_borrowers import B_DUE
from tests.helpers.scripted_clients import ScriptedKB, ScriptedLLM

FLOWS = load_all_flows()


def _transcript_for_emotion(emotion: str) -> str:
    mapping = {
        "fear": "I can't pay, I'm really scared and afraid",
        "anger": "I can't pay, stop calling me, I'm angry and frustrated",
        "neutral": "I can't pay this month",
        "hopelessness": "I can't go on, there is no hope left, koi ummeed nahi bahut hopeless",
    }
    return mapping[emotion]


def test_cant_pay_fear_anger_neutral_select_different_tone_registers():
    fear = classify_emotion_rules(_transcript_for_emotion("fear"))
    anger = classify_emotion_rules(_transcript_for_emotion("anger"))
    neutral = classify_emotion_rules(_transcript_for_emotion("neutral"))

    fear_register = select_tone_register("30-60", fear.emotion, fear.intensity)
    anger_register = select_tone_register("30-60", anger.emotion, anger.intensity)
    neutral_register = select_tone_register("30-60", neutral.emotion, neutral.intensity)

    assert fear.emotion == "fear"
    assert anger.emotion in ("anger", "frustration")
    assert neutral.emotion == "neutral"
    assert fear_register == "reassure"
    assert anger_register == "de_escalate"
    assert neutral_register == "firm"

    transcripts = (
        _transcript_for_emotion("fear"),
        _transcript_for_emotion("anger"),
        _transcript_for_emotion("neutral"),
    )
    replies: list[str] = []
    for transcript in transcripts:
        state = new_conversation_state("c", "default", "b")
        state.slots["comms_prefs"] = {"language": "en"}
        state.slots["bucket"] = "30-60"
        state = apply_emotion_to_state(state, classify_emotion_rules(transcript))
        replies.append(render("clarify_general", state, FLOWS, locale="en-IN", channel="whatsapp"))

    assert len(set(replies)) == 3
    for reply in replies:
        lowered = reply.lower()
        assert "police" not in lowered
        assert "threaten" not in lowered
        assert "emi" in lowered or "pay" in lowered


def test_high_intensity_hopelessness_triggers_safety_preempt():
    classification = classify_emotion_rules(_transcript_for_emotion("hopelessness"))
    assert classification.emotion == "hopelessness"
    assert classification.intensity == "high"
    assert emotion_triggers_safety(classification)

    state = new_conversation_state("c", "default", "b")
    state = apply_emotion_to_state(state, classification)
    cfg = tenant_config("default")
    result = safety_preempt(
        _transcript_for_emotion("hopelessness"),
        state,
        cfg,
        emotion_label=state.slots["emotion"],
        emotion_intensity=state.slots["emotion_intensity"],
    )
    assert result is not None
    assert result.transfer_to_human is True
    assert result.suspend_recovery is True
    assert result.reason == "emotion_hopelessness_high"


@pytest.mark.asyncio
async def test_hopelessness_routes_human_via_handle_turn():
    memory = InMemoryMemoryStore()
    await memory.save_borrower(
        BorrowerRecord(
            borrower_id=B_DUE,
            loan={"amount_due": 5000, "dpd": 45, "bucket": "30-60"},
        )
    )
    response = await handle_turn(
        TurnRequest(
            call_id="emotion-crisis",
            tenant_id="default",
            borrower_id=B_DUE,
            transcript=_transcript_for_emotion("hopelessness"),
            turn_meta={"call_date": "2026-06-25"},
        ),
        memory=memory,
        kb=ScriptedKB([]),
        llm=ScriptedLLM([]),
        tools=FakeToolClient(),
    )
    assert response.transfer_to_human is True
    assert response.reply_text
    borrower = await memory.load_borrower(B_DUE)
    assert borrower is not None
    assert borrower.emotions[-1]["emotion"] == "hopelessness"
    assert borrower.emotions[-1]["intensity"] == "high"


def test_emotion_distinct_from_intent_axis():
    angry = classify_emotion_rules("Main bahut gussa hoon, band karo calling!")
    calm_refusal = classify_emotion_rules("Theek hai. Payment nahi hoga. Main shaant hoon.")

    assert angry.emotion in ("anger", "frustration")
    assert calm_refusal.emotion == "neutral"

    willing_command = Command(command="start_flow", flow="promise_to_pay")
    refusal_command = Command(command="clarify", reason="refusal")
    assert willing_command.flow == "promise_to_pay"
    assert refusal_command.command == "clarify"
    assert angry.emotion != calm_refusal.emotion


def test_emotion_written_to_history_and_available_to_persona_risk():
    state = new_conversation_state("c", "default", "b")
    classification = classify_emotion_rules("Main bahut darr lag raha hai")
    state = apply_emotion_to_state(state, classification)
    borrower = BorrowerRecord(borrower_id="b")
    updated = sync_emotion_on_persist(borrower, state=state)
    assert updated.emotions[-1]["emotion"] == "fear"
    assert updated.emotions[-1]["intensity"] in ("low", "med", "high")
    assert updated.emotions[-1]["channel"] == "text"

    hydrated = hydrate_from_borrower(state, updated)
    assert hydrated.slots["emotion"] == "fear"


def test_determinism_fixed_inputs():
    transcript = "I can't pay, I'm really scared"
    first = classify_emotion_rules(transcript)
    second = classify_emotion_rules(transcript)
    assert first.model_dump() == second.model_dump()


def test_emotion_is_input_not_license_constant():
    assert EMOTION_IS_INPUT_NOT_LICENSE is True


@pytest.mark.compliance
def test_anger_emotion_does_not_relax_gate():
    state = new_conversation_state("c", "default", "b")
    state.slots["emotion"] = "anger"
    state.slots["emotion_intensity"] = "high"
    state.slots["tone_register"] = "de_escalate"
    cfg = tenant_config("default")
    now = datetime(2026, 6, 25, 10, 0, tzinfo=UTC)
    result = gate("Police aa jayegi agar payment nahi", state, cfg, now=now)
    assert result.verdict == "modify"
    assert result.level == "CRITICAL"
    assert "police" not in result.text.lower()


def test_prosody_hook_parsed_without_error():
    classification = classify_emotion_from_turn(
        "Theek hai",
        turn_meta={"prosody": {"pitch_variance": 0.8, "energy": 0.9, "speech_rate": 1.1}},
        channel="voice",
    )
    assert classification.emotion in classify_emotion_rules("Theek hai").emotion


@pytest.mark.asyncio
async def test_llm_rubric_parses_valid_json():
    llm = ScriptedLLM(['{"emotion":"fear","intensity":"med","confidence":0.77}'])
    result = await classify_emotion_llm("test", llm=llm)
    assert result.emotion == "fear"
    assert result.intensity == "med"
    assert result.source == "llm_rubric"


def test_parse_rejects_unknown_emotion():
    raw = '{"emotion":"rage","intensity":"high","confidence":0.9}'
    assert parse_and_validate_emotion(raw) is None


@pytest.mark.asyncio
async def test_handle_turn_does_not_add_second_llm_call():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:promise_to_pay]] kal"}])
    llm = ScriptedLLM(
        [
            [
                {"command": "start_flow", "flow": "promise_to_pay"},
                {"command": "set_slot", "name": "ptp_date", "value": "2026-06-27"},
            ]
        ]
    )
    await memory.save_borrower(
        BorrowerRecord(
            borrower_id=B_DUE,
            loan={"amount_due": 5000, "dpd": 45, "bucket": "30-60"},
        )
    )
    for index in range(5):
        await handle_turn(
            TurnRequest(
                call_id=f"emotion-lat-{index}",
                tenant_id="default",
                borrower_id=B_DUE,
                transcript="kal payment kar dunga",
                turn_meta={"call_date": "2026-06-25"},
            ),
            memory=memory,
            kb=kb,
            llm=llm,
            tools=FakeToolClient(),
        )
    assert llm.call_count == 5
