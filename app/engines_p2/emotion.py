"""Emotion Engine (Sprint 11 / blueprint Engine 3).

Detects dominant emotion + intensity per turn from transcript (text channel first).
Emotion biases NLG tone and can route high-intensity hopelessness into safety_preempt.
Emotion is an input — it is NOT a license to bypass the compliance gate.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from app.schemas.emotion import EmotionClassification
from app.schemas.state import BorrowerRecord, ConversationState, Event

logger = logging.getLogger(__name__)

# Emotion adjusts tone but never relaxes compliance — never wire into gate bypass paths.
EMOTION_IS_INPUT_NOT_LICENSE = True

# DECISION NEEDED: fine-tuned small model vs Gemini rubric (v1: rules on hot path; rubric optional).
EMOTION_LLM_RUBRIC = True

EMOTION_IDS: frozenset[str] = frozenset(
    {
        "anxiety",
        "fear",
        "stress",
        "anger",
        "frustration",
        "hopelessness",
        "shame",
        "remorse",
        "confusion",
        "optimism",
        "neutral",
    }
)

INTENSITY_LEVELS: frozenset[str] = frozenset({"low", "med", "high"})

EMOTION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "fear": ("darr", "dar lag", "afraid", "fear", "dar hai", "scared", "khauf"),
    "anger": ("gussa", "angry", "gusse", "chilla", "harassment", "band karo", "pagal"),
    "hopelessness": (
        "hopeless",
        "hopelessness",
        "ummeed nahi",
        "koi ummeed",
        "kuch nahi bacha",
        "give up",
        "khatam ho gaya",
        "bikhar gay",
    ),
    "anxiety": ("anxiety", "chinta", "anxious", "fikar", "bechain"),
    "stress": ("stress", "tension", "pressure", "bahut tension", "overload"),
    "frustration": ("frustrat", "pareshaan", "irritat", "annoyed", "fed up"),
    "shame": ("sharam", "shame", "embarrass", "bezati", "sharminda"),
    "remorse": ("sorry", "maafi", "guilt", "pachhtava", "regret"),
    "confusion": ("confus", "samajh nahi", "kya matlab", "clear nahi", "pata nahi kya"),
    "optimism": ("pakka", "sure", "ho jayega", "kar dunga", "will pay", "try karunga"),
}

HIGH_INTENSITY_MARKERS: tuple[str, ...] = (
    "bahut",
    "very",
    "extremely",
    "totally",
    "bilkul",
    "can't take",
    "cannot take",
    "mar ja",
    "no hope",
    "koi ummeed nahi",
)

MED_INTENSITY_MARKERS: tuple[str, ...] = (
    "thoda",
    "somewhat",
    "quite",
    "kaafi",
    "really",
)

# Blueprint §8.3 — bucket sets baseline register; emotion adjusts it.
BUCKET_BASELINE_REGISTER: dict[str, str] = {
    "current": "standard",
    "0-30": "standard",
    "30-60": "firm",
    "60-90": "firm",
    "90+": "serious",
    "B0": "standard",
    "B1": "standard",
    "B2": "firm",
}

EMOTION_REGISTER_ADJUSTMENT: dict[str, str] = {
    "anger": "de_escalate",
    "frustration": "de_escalate",
    "fear": "reassure",
    "anxiety": "reassure",
    "stress": "reassure",
    "shame": "dignity",
    "remorse": "dignity",
    "hopelessness": "care",
    "confusion": "clarify_tone",
    "optimism": "encouraging",
    "neutral": "standard",
}


class ProsodyFeatures:
    """Seam for voice layer — acoustic features supplied via turn_meta.prosody."""

    __slots__ = ("pitch_variance", "speech_rate", "energy", "raw")

    def __init__(
        self,
        *,
        pitch_variance: float | None = None,
        speech_rate: float | None = None,
        energy: float | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        self.pitch_variance = pitch_variance
        self.speech_rate = speech_rate
        self.energy = energy
        self.raw = raw or {}


def parse_prosody(raw: Any) -> ProsodyFeatures | None:
    """Parse turn_meta.prosody for future voice-channel fusion (out of scope for v1)."""
    if not isinstance(raw, dict) or not raw:
        return None
    return ProsodyFeatures(
        pitch_variance=_coerce_float(raw.get("pitch_variance")),
        speech_rate=_coerce_float(raw.get("speech_rate")),
        energy=_coerce_float(raw.get("energy")),
        raw=dict(raw),
    )


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text_hits(text: str, keywords: tuple[str, ...]) -> int:
    lowered = text.lower()
    return sum(1 for keyword in keywords if keyword in lowered)


def _score_emotions(text: str) -> dict[str, float]:
    scores = {emotion: 0.0 for emotion in EMOTION_IDS}
    for emotion, keywords in EMOTION_KEYWORDS.items():
        hits = _text_hits(text, keywords)
        if hits:
            scores[emotion] = float(hits) * 0.45
    if not any(score > 0 for score in scores.values()):
        scores["neutral"] = 0.55
    return scores


def _intensity_from_text(text: str, *, top_score: float) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in HIGH_INTENSITY_MARKERS) or "!" in text:
        return "high"
    if top_score >= 0.9 or any(marker in lowered for marker in MED_INTENSITY_MARKERS):
        return "med"
    return "low"


def _apply_prosody_boost(
    scores: dict[str, float],
    prosody: ProsodyFeatures | None,
) -> dict[str, float]:
    """Optional prosody hook — v1 leaves scores unchanged unless features are present."""
    if prosody is None:
        return scores
    adjusted = dict(scores)
    if prosody.energy is not None and prosody.energy >= 0.75:
        adjusted["anger"] = adjusted.get("anger", 0.0) + 0.15
        adjusted["frustration"] = adjusted.get("frustration", 0.0) + 0.1
    if prosody.pitch_variance is not None and prosody.pitch_variance >= 0.7:
        adjusted["fear"] = adjusted.get("fear", 0.0) + 0.1
        adjusted["anxiety"] = adjusted.get("anxiety", 0.0) + 0.1
    return adjusted


def classify_emotion_rules(
    transcript: str,
    *,
    prosody: ProsodyFeatures | None = None,
    channel: str = "text",
) -> EmotionClassification:
    """Pure deterministic classifier — used on the live turn path and in CI."""
    text = transcript.strip()
    scores = _apply_prosody_boost(_score_emotions(text), prosody)
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    emotion, top_score = ranked[0]
    if top_score < 0.35:
        emotion = "neutral"
        top_score = 0.45
    intensity = _intensity_from_text(text, top_score=top_score)
    confidence = min(0.95, max(0.45, top_score if top_score > 0 else 0.5))
    return EmotionClassification(
        emotion=emotion,
        intensity=intensity,
        confidence=round(confidence, 3),
        channel=channel,
        source="rules",
    )


def classify_emotion_from_turn(
    transcript: str,
    *,
    turn_meta: dict[str, Any] | None = None,
    channel: str = "voice",
) -> EmotionClassification:
    """Classify emotion for a turn, optionally fusing turn_meta.prosody when supplied."""
    meta = turn_meta or {}
    prosody = parse_prosody(meta.get("prosody"))
    return classify_emotion_rules(transcript, prosody=prosody, channel=channel)


def select_tone_register(
    bucket: str | None,
    emotion: str,
    intensity: str,
) -> str:
    """Bucket baseline + emotion adjustment (blueprint §8.3).

    Tone may become more serious, never aggressive.
    """
    baseline = BUCKET_BASELINE_REGISTER.get(str(bucket or ""), "standard")
    if emotion == "neutral":
        return baseline
    adjustment = EMOTION_REGISTER_ADJUSTMENT.get(emotion, "standard")
    if adjustment != "standard":
        return adjustment
    if intensity == "low":
        return baseline
    return baseline


def emotion_triggers_safety(classification: EmotionClassification) -> bool:
    """High-intensity hopelessness routes into Sprint 6 safety_preempt."""
    return classification.emotion == "hopelessness" and classification.intensity == "high"


def emotion_to_slot(
    classification: EmotionClassification,
    *,
    ts: str,
    bucket: str | None,
) -> dict[str, Any]:
    tone_register = select_tone_register(bucket, classification.emotion, classification.intensity)
    return {
        "emotion": classification.emotion,
        "emotion_intensity": classification.intensity,
        "confidence": classification.confidence,
        "tone_register": tone_register,
        "channel": classification.channel,
        "source": classification.source,
        "ts": ts,
    }


def apply_emotion_to_state(
    state: ConversationState,
    classification: EmotionClassification,
    *,
    ts: str | None = None,
) -> ConversationState:
    """Write live-turn emotion slots and append an emotion event."""
    stamp = ts or datetime.now(UTC).isoformat()
    updated = state.model_copy(deep=True)
    bucket = updated.slots.get("bucket")
    if isinstance(bucket, dict):
        bucket = bucket.get("name")
    slot = emotion_to_slot(classification, ts=stamp, bucket=str(bucket) if bucket else None)
    slots = dict(updated.slots)
    slots["emotion"] = classification.emotion
    slots["emotion_intensity"] = classification.intensity
    slots["tone_register"] = slot["tone_register"]
    slots["emotion_detail"] = slot
    updated.slots = slots
    updated.events.append(
        Event(
            ts=stamp,
            kind="emotion",
            data={
                "emotion": classification.emotion,
                "label": classification.emotion,
                "intensity": classification.intensity,
                "confidence": classification.confidence,
                "tone_register": slot["tone_register"],
                "channel": classification.channel,
                "source": classification.source,
            },
        )
    )
    return updated


def build_emotion_system_prompt() -> str:
    emotions = ", ".join(sorted(EMOTION_IDS))
    return (
        "You classify borrower emotion for tone selection only. "
        "Output ONLY a JSON object with keys: emotion, intensity, confidence. "
        f"emotion MUST be one of: {emotions}. "
        "intensity MUST be low, med, or high. confidence is 0.0-1.0. "
        "Emotion is separate from payment intent — do not infer willingness. "
        "Do NOT recommend policy actions or outbound language."
    )


def build_emotion_user_prompt(context: dict[str, Any]) -> str:
    return json.dumps(context, ensure_ascii=False)


def parse_and_validate_emotion(raw: str) -> EmotionClassification | None:
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("emotion: invalid JSON from LLM")
        return None
    if not isinstance(data, dict):
        return None
    emotion = data.get("emotion")
    if emotion not in EMOTION_IDS:
        logger.warning("emotion: rejected unknown emotion %s", emotion)
        return None
    intensity = data.get("intensity", "med")
    if intensity not in INTENSITY_LEVELS:
        intensity = "med"
    try:
        confidence = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))
    try:
        return EmotionClassification(
            emotion=str(emotion),
            intensity=str(intensity),
            confidence=confidence,
            channel=str(data.get("channel", "text")),
            source="llm_rubric",
        )
    except ValidationError:
        return None


async def classify_emotion_llm(
    transcript: str,
    *,
    turn_meta: dict[str, Any] | None = None,
    channel: str = "voice",
    llm: Any,
) -> EmotionClassification:
    """Optional rubric classifier — NOT called on the blocking handle_turn path."""
    context = {
        "transcript": transcript,
        "prosody": turn_meta.get("prosody") if turn_meta else None,
        "channel": channel,
    }
    raw = await llm.complete(
        build_emotion_system_prompt(),
        build_emotion_user_prompt(context),
        json_only=True,
    )
    parsed = parse_and_validate_emotion(raw)
    if parsed is None:
        fallback = classify_emotion_from_turn(transcript, turn_meta=turn_meta, channel=channel)
        fallback.source = "rules_fallback"
        return fallback
    return parsed


def sync_emotion_on_persist(
    borrower: BorrowerRecord,
    *,
    state: ConversationState,
    trigger: str = "turn_persist",
) -> BorrowerRecord:
    """Append turn emotion to durable borrower history for persona/risk engines."""
    emotion = state.slots.get("emotion")
    if not emotion:
        return borrower
    intensity = state.slots.get("emotion_intensity", "med")
    detail = state.slots.get("emotion_detail") or {}
    stamp = str(detail.get("ts") or datetime.now(UTC).isoformat())
    updated = borrower.model_copy(deep=True)
    entry = {
        "ts": stamp,
        "date": stamp[:10],
        "emotion": str(emotion),
        "label": str(emotion),
        "intensity": str(intensity),
        "channel": str(detail.get("channel", "text")),
        "tone_register": detail.get("tone_register"),
        "confidence": detail.get("confidence"),
        "trigger": trigger,
    }
    if updated.emotions and updated.emotions[-1].get("ts") == stamp:
        return updated
    updated.emotions = [*updated.emotions, entry]
    return updated
