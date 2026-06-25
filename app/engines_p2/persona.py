"""Persona Engine (Sprint 10 / blueprint Engine 2).

Classifies borrowers into 15 blueprint personas using trust/risk signals and history.
Persona is an input to decision/NLG — it is NOT a license to bypass the compliance gate.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from app.engines_p2.risk import cluster_excuse
from app.schemas.persona import PersonaClassification
from app.schemas.state import BorrowerRecord, ConversationState

logger = logging.getLogger(__name__)

# Persona is a hypothesis, not a license — never wire into gate bypass paths.
PERSONA_IS_INPUT_NOT_LICENSE = True

# DECISION NEEDED: fine-tuned small model vs Gemini rubric (v1: rubric via LLMClient).
PERSONA_LLM_RUBRIC = True

PERSONA_IDS: frozenset[str] = frozenset(
    {
        "genuine_payer",
        "forgetful",
        "salary_dependent",
        "temporary_hardship",
        "chronic_tomorrow",
        "promise_breaker",
        "strategic_defaulter",
        "settlement_hunter",
        "genuine_settlement_candidate",
        "ghost",
        "angry",
        "dispute",
        "fraud_claimant",
        "wrong_number",
        "vulnerable",
    }
)

WRONG_NUMBER_KEYWORDS = ("wrong number", "galat number", "not me", "wrong person")
SETTLEMENT_KEYWORDS = ("settlement", "one time", "ots", "discount", "kam kar do")
TOMORROW_KEYWORDS = ("kal", "tomorrow", "parso", "next week")
ANGRY_KEYWORDS = ("angry", "gussa", "harassment", "stop calling", "band karo")

ABILITY_WILLINGNESS_MATRIX = {
    ("high", "high"): "genuine_payer",
    ("high", "low"): "strategic_defaulter",
    ("low", "high"): "temporary_hardship",
    ("low", "low"): "chronic_tomorrow",
}


def _parse_ts(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _active_risk_flags(borrower: BorrowerRecord, *, min_confidence: float = 0.5) -> set[str]:
    return {
        str(flag["flag"])
        for flag in borrower.risk_flags
        if float(flag.get("confidence", 0.0)) >= min_confidence
    }


def _broken_ptp_count(borrower: BorrowerRecord) -> int:
    seen: set[str] = set()
    count = 0
    for bp in borrower.broken_ptps:
        key = str(bp.get("promised_date", ""))[:10]
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        count += 1
    for ptp in borrower.ptps:
        if str(ptp.get("status", "")).lower() != "broken":
            continue
        key = str(ptp.get("promised_date", ""))[:10]
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        count += 1
    return count


def _kept_ptp_count(borrower: BorrowerRecord) -> int:
    return sum(1 for ptp in borrower.ptps if str(ptp.get("status", "")).lower() == "kept")


def _has_partials(borrower: BorrowerRecord) -> bool:
    return any(payment.get("partial") for payment in borrower.payments)


def _text_hits(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in keywords)


def _ability_quadrant(borrower: BorrowerRecord) -> str:
    trust = borrower.trust_current
    full_payments = sum(
        1
        for payment in borrower.payments
        if payment.get("full", True) and not payment.get("partial")
    )
    if trust >= 75 or full_payments >= 3:
        return "high"
    if trust >= 45 or full_payments >= 1:
        return "medium"
    return "low"


def _willingness_quadrant(borrower: BorrowerRecord, risk_flags: set[str]) -> str:
    if risk_flags & {"strategic_default", "promise_breaking", "excuse_recycling"}:
        return "low"
    if _broken_ptp_count(borrower) >= 2:
        return "low"
    if _kept_ptp_count(borrower) >= 2 and borrower.trust_current >= 60:
        return "high"
    return "medium"


def _salary_payday_pattern(borrower: BorrowerRecord) -> bool:
    salary_excuses = sum(
        1
        for excuse in borrower.excuses
        if cluster_excuse(str(excuse.get("text", ""))) == "salary_delay"
    )
    payment_days: list[int] = []
    for payment in borrower.payments:
        ts = _parse_ts(payment.get("date") or payment.get("ts"))
        if ts:
            payment_days.append(ts.day)
    if len(payment_days) >= 2:
        spread = max(payment_days) - min(payment_days)
        if spread <= 3 and salary_excuses >= 1:
            return True
    return salary_excuses >= 2


def _recent_emotion_labels(state: ConversationState | None, limit: int = 3) -> list[str]:
    if state is None:
        return []
    labels: list[str] = []
    for event in reversed(state.events):
        if event.kind != "emotion":
            continue
        label = str(event.data.get("label") or event.data.get("emotion") or "")
        if label:
            labels.append(label.lower())
        if len(labels) >= limit:
            break
    return labels


def _recent_turn_summaries(state: ConversationState | None, limit: int = 2) -> list[dict[str, Any]]:
    if state is None:
        return []
    turns: list[dict[str, Any]] = []
    for event in reversed(state.events):
        if event.kind in ("turn", "command"):
            turns.append({"kind": event.kind, "data": event.data})
        if len(turns) >= limit:
            break
    return list(reversed(turns))


def build_persona_context(
    borrower: BorrowerRecord,
    state: ConversationState | None = None,
) -> dict[str, Any]:
    """Assemble classifier inputs; reuses stored trust/risk — does not recompute them."""
    risk_flags = _active_risk_flags(borrower)
    ability = _ability_quadrant(borrower)
    willingness = _willingness_quadrant(borrower, risk_flags)
    return {
        "trust_current": borrower.trust_current,
        "risk_flags": [
            {"flag": f["flag"], "confidence": f.get("confidence"), "reason": f.get("reason")}
            for f in borrower.risk_flags
        ],
        "ability_quadrant": ability,
        "willingness_quadrant": willingness,
        "kept_ptps": _kept_ptp_count(borrower),
        "broken_ptps": _broken_ptp_count(borrower),
        "has_partials": _has_partials(borrower),
        "hardships": borrower.hardships,
        "disputes": borrower.disputes,
        "emotions": borrower.emotions[-3:],
        "compliance_flags": borrower.compliance_flags,
        "recent_turns": _recent_turn_summaries(state),
        "recent_emotions": _recent_emotion_labels(state),
        "notes": [note for note in borrower.notes if note.get("kind") != "trust_event"][-5:],
    }


def _score_candidates(
    borrower: BorrowerRecord,
    *,
    state: ConversationState | None,
) -> dict[str, float]:
    """Deterministic persona scores from blueprint signals."""
    scores: dict[str, float] = {persona: 0.0 for persona in PERSONA_IDS}
    risk = _active_risk_flags(borrower)
    trust = borrower.trust_current
    broken = _broken_ptp_count(borrower)
    kept = _kept_ptp_count(borrower)
    ability = _ability_quadrant(borrower)
    willingness = _willingness_quadrant(borrower, risk)

    if borrower.compliance_flags.get("vulnerable"):
        scores["vulnerable"] += 0.95
    for note in borrower.notes:
        if _text_hits(str(note.get("text", "")), WRONG_NUMBER_KEYWORDS):
            scores["wrong_number"] += 0.9

    if "fraud_indicator" in risk:
        scores["fraud_claimant"] += 0.88
    if borrower.disputes or borrower.compliance_flags.get("dispute_hold"):
        scores["dispute"] += 0.85
    if "ghosting" in risk:
        scores["ghost"] += 0.82

    if borrower.hardships or any(
        cluster_excuse(str(e.get("text", ""))) in ("medical", "job_loss", "family_emergency")
        for e in borrower.excuses
    ):
        if _has_partials(borrower):
            scores["temporary_hardship"] += 0.86 if "strategic_default" not in risk else 0.55

    if "strategic_default" in risk:
        scores["strategic_defaulter"] += 0.84
    elif ability == "high" and willingness == "low" and broken >= 2:
        scores["strategic_defaulter"] += 0.7

    if "settlement_fishing" in risk:
        scores["settlement_hunter"] += 0.8
    if any(_text_hits(str(n.get("text", "")), SETTLEMENT_KEYWORDS) for n in borrower.notes):
        if borrower.hardships and _has_partials(borrower) and "strategic_default" not in risk:
            scores["genuine_settlement_candidate"] += 0.78
        else:
            scores["settlement_hunter"] += 0.45

    if broken >= 3 or ("promise_breaking" in risk and broken >= 2):
        scores["promise_breaker"] += 0.88
    elif broken == 2:
        scores["promise_breaker"] += 0.65

    tomorrow_signals = sum(
        1
        for ptp in borrower.ptps
        if any(k in str(ptp.get("promised_date", "")).lower() for k in TOMORROW_KEYWORDS)
    ) + sum(
        1 for note in borrower.notes if _text_hits(str(note.get("text", "")), TOMORROW_KEYWORDS)
    )
    if broken >= 2 and (tomorrow_signals >= 1 or len(borrower.ptps) >= 3):
        scores["chronic_tomorrow"] += 0.8

    if _salary_payday_pattern(borrower):
        scores["salary_dependent"] += 0.85

    angry_emotions = borrower.emotions + [
        {"label": label} for label in _recent_emotion_labels(state)
    ]
    if any(
        _text_hits(str(item.get("label", item.get("text", ""))), ANGRY_KEYWORDS)
        for item in angry_emotions
    ):
        scores["angry"] += 0.8

    if trust >= 82 and kept >= 2 and broken == 0:
        scores["genuine_payer"] += 0.9
    elif trust >= 70 and kept >= 1 and broken <= 1:
        scores["genuine_payer"] += 0.65

    if 55 <= trust <= 78 and broken == 1 and kept >= 1:
        scores["forgetful"] += 0.75
    elif broken == 1 and kept >= 1:
        scores["forgetful"] += 0.55

    matrix_hint = ABILITY_WILLINGNESS_MATRIX.get((ability, willingness))
    if matrix_hint and scores[matrix_hint] < 0.5:
        scores[matrix_hint] += 0.35

    return scores


def classify_persona_rules(
    borrower: BorrowerRecord,
    *,
    state: ConversationState | None = None,
) -> PersonaClassification:
    """Pure deterministic classifier — used on persist path and in CI."""
    scores = _score_candidates(borrower, state=state)
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    primary, primary_score = ranked[0]
    secondary = ranked[1][0] if len(ranked) > 1 and ranked[1][1] > 0.25 else None
    secondary_score = ranked[1][1] if secondary else 0.0

    if primary_score < 0.35:
        primary = "forgetful"
        primary_score = 0.45
        secondary = secondary or "genuine_payer"

    confidence = min(0.95, max(0.45, primary_score))
    blend: dict[str, float] = {primary: round(confidence, 3)}
    if secondary and secondary_score > 0.2:
        secondary_weight = round(min(0.45, secondary_score * 0.5), 3)
        blend[primary] = round(confidence - secondary_weight * 0.5, 3)
        blend[secondary] = secondary_weight

    ability = _ability_quadrant(borrower)
    willingness = _willingness_quadrant(borrower, _active_risk_flags(borrower))
    return PersonaClassification(
        primary_persona=primary,
        secondary_persona=secondary,
        confidence=round(confidence, 3),
        blend=blend,
        ability=ability,
        willingness=willingness,
        source="rules",
    )


def build_persona_system_prompt() -> str:
    personas = ", ".join(sorted(PERSONA_IDS))
    return (
        "You classify collection borrowers into personas for strategy/tone selection only. "
        "Output ONLY a JSON object with keys: primary_persona, secondary_persona, confidence. "
        f"primary_persona and secondary_persona MUST be from: {personas}. "
        "secondary_persona may be null. confidence is 0.0-1.0. "
        "Use trust_current and risk_flags as given — do not invent payment history. "
        "Persona is a hypothesis overridable by fresh evidence — not a sticky label. "
        "Do NOT recommend policy actions or outbound language."
    )


def build_persona_user_prompt(context: dict[str, Any]) -> str:
    return json.dumps(context, ensure_ascii=False)


def parse_and_validate_persona(raw: str) -> PersonaClassification | None:
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("persona: invalid JSON from LLM")
        return None
    if not isinstance(data, dict):
        return None
    primary = data.get("primary_persona")
    if primary not in PERSONA_IDS:
        logger.warning("persona: rejected unknown primary %s", primary)
        return None
    secondary = data.get("secondary_persona")
    if secondary is not None and secondary not in PERSONA_IDS:
        secondary = None
    try:
        confidence = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))
    blend: dict[str, float] = {str(primary): confidence}
    if secondary:
        blend[str(secondary)] = round(max(0.1, (1.0 - confidence) * 0.6), 3)
    try:
        return PersonaClassification(
            primary_persona=str(primary),
            secondary_persona=str(secondary) if secondary else None,
            confidence=confidence,
            blend=blend,
            ability=data.get("ability_quadrant"),
            willingness=data.get("willingness_quadrant"),
            source="llm_rubric",
        )
    except ValidationError:
        return None


async def classify_persona_llm(
    borrower: BorrowerRecord,
    *,
    state: ConversationState | None = None,
    llm: Any,
) -> PersonaClassification:
    """Optional rubric classifier — NOT called on the blocking handle_turn path."""
    context = build_persona_context(borrower, state)
    raw = await llm.complete(
        build_persona_system_prompt(),
        build_persona_user_prompt(context),
        json_only=True,
    )
    parsed = parse_and_validate_persona(raw)
    if parsed is None:
        fallback = classify_persona_rules(borrower, state=state)
        fallback.source = "rules_fallback"
        return fallback
    return parsed


def persona_to_slot(persona: PersonaClassification, *, ts: str) -> dict[str, Any]:
    return {
        "primary_persona": persona.primary_persona,
        "secondary_persona": persona.secondary_persona,
        "confidence": persona.confidence,
        "blend": persona.blend,
        "ability": persona.ability,
        "willingness": persona.willingness,
        "source": persona.source,
        "ts": ts,
    }


def _transition_trigger(
    borrower: BorrowerRecord,
    persona: PersonaClassification,
    *,
    state: ConversationState | None,
) -> str:
    if state and state.events:
        last = state.events[-1]
        if last.kind == "turn":
            return "turn_complete"
    if _active_risk_flags(borrower):
        return f"risk:{next(iter(_active_risk_flags(borrower)))}"
    if borrower.trust_current != 50:
        return f"trust:{borrower.trust_current}"
    return "history_refresh"


def refresh_borrower_persona(
    borrower: BorrowerRecord,
    *,
    state: ConversationState | None = None,
    trigger: str | None = None,
    ts: datetime | None = None,
) -> BorrowerRecord:
    """Recompute persona (rules-only on persist) and log transitions."""
    stamp = (ts or datetime.now(UTC)).isoformat()
    classification = classify_persona_rules(borrower, state=state)
    slot = persona_to_slot(classification, ts=stamp)
    updated = borrower.model_copy(deep=True)
    prior_primary = str(updated.persona_current.get("primary_persona", ""))
    new_primary = classification.primary_persona

    if prior_primary and prior_primary != new_primary:
        updated.persona_history = [
            *updated.persona_history,
            {
                "ts": stamp,
                "from": prior_primary,
                "to": new_primary,
                "secondary": classification.secondary_persona,
                "trigger": trigger or _transition_trigger(borrower, classification, state=state),
                "confidence": classification.confidence,
            },
        ]

    updated.persona_current = slot
    return updated


def apply_persona_to_state(state: ConversationState, borrower: BorrowerRecord) -> ConversationState:
    """Expose cached persona on live call slots — read-only input for decision/NLG."""
    updated = state.model_copy(deep=True)
    slots = dict(updated.slots)
    slots["persona"] = dict(borrower.persona_current) if borrower.persona_current else {}
    updated.slots = slots
    return updated


def sync_persona_on_persist(
    borrower: BorrowerRecord,
    *,
    state: ConversationState | None = None,
    trigger: str = "turn_persist",
) -> BorrowerRecord:
    """Deterministic persona refresh during persist — no LLM on hot path."""
    return refresh_borrower_persona(borrower, state=state, trigger=trigger)
