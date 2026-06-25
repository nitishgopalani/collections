"""Decision objective-function overlay (Sprint 12 / blueprint Engine 8 full).

Ranks executor-allowable actions using cached Phase-2 signals. The overlay biases
which compliant action to prefer — it never invents actions, bypasses flows, or
overrides the compliance gate.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.engine.hardship import hardship_context_active
from app.engines_p2.recovery_prob import recovery_effort_boost
from app.schemas.decision import DecisionCandidate, DecisionOverlayResult, DecisionSignals
from app.schemas.flow import FlowBranch, FlowSet, FlowStep
from app.schemas.state import ConversationState, Event

# Overlay biases decisions; compliance gate retains absolute veto.
OVERLAY_IS_INPUT_NOT_GATE = True

# λ2 is effectively infinite — non-compliant candidates are excluded, never scored.
LAMBDA_CONTACT = 0.15
LAMBDA_COMPLIANCE = float("inf")
LAMBDA_EXPERIENCE = 0.25

QUADRANTS = frozenset({"CAN_WILL", "WANTS_CANT", "CAN_WONT", "CANT_WONT"})

HUMAN_OWNED_ACTIONS: frozenset[str] = frozenset(
    {
        "settlement_review",
        "restructure_review",
        "moratorium_review",
        "legal_prep",
        "vulnerable_case_review",
    }
)

AGGRESSIVE_PRESSURE_ACTIONS: frozenset[str] = frozenset(
    {
        "ask_earlier_date",
        "apply_collection_pressure",
        "escalate_dunning",
    }
)

ACTION_CATEGORY: dict[str, str] = {
    "create_payment_link": "frictionless_payment",
    "validate_ptp": "forward_ptp",
    "schedule_followup": "forward_ptp",
    "ask_earlier_date": "aggressive_pressure",
    "route_vulnerable": "empathy_handoff",
    "raise_dispute_ticket": "firm_factual",
    "verify_payment": "firm_factual",
    "offer_partial_payment": "partial_ptp",
    "forward_ptp_empathy": "empathy_partial",
    "settlement_review": "human_settlement",
    "restructure_review": "human_restructure",
    "moratorium_review": "human_restructure",
    "diagnose_hardship": "diagnose",
    "behavioral_risk_watch": "firm_factual",
}

CATEGORY_RECOVERY: dict[str, float] = {
    "frictionless_payment": 0.95,
    "forward_ptp": 0.75,
    "partial_ptp": 0.55,
    "empathy_partial": 0.45,
    "firm_factual": 0.65,
    "aggressive_pressure": 0.7,
    "empathy_handoff": 0.1,
    "diagnose": 0.35,
    "human_settlement": 0.5,
    "human_restructure": 0.4,
}

CATEGORY_CONTACT_COST: dict[str, float] = {
    "frictionless_payment": 0.2,
    "forward_ptp": 0.35,
    "partial_ptp": 0.4,
    "empathy_partial": 0.45,
    "firm_factual": 0.5,
    "aggressive_pressure": 0.8,
    "empathy_handoff": 0.6,
    "diagnose": 0.55,
    "human_settlement": 0.7,
    "human_restructure": 0.75,
}

CATEGORY_EXPERIENCE_COST: dict[str, float] = {
    "frictionless_payment": 0.15,
    "forward_ptp": 0.25,
    "partial_ptp": 0.2,
    "empathy_partial": 0.15,
    "firm_factual": 0.35,
    "aggressive_pressure": 0.95,
    "empathy_handoff": 0.1,
    "diagnose": 0.3,
    "human_settlement": 0.25,
    "human_restructure": 0.25,
}

QUADRANT_PREFERRED: dict[str, tuple[str, ...]] = {
    "CAN_WILL": ("frictionless_payment", "forward_ptp"),
    "WANTS_CANT": ("empathy_partial", "partial_ptp", "forward_ptp"),
    "CAN_WONT": ("firm_factual", "behavioral_risk_watch"),
    "CANT_WONT": ("diagnose", "human_settlement", "human_restructure"),
}


def extract_signals(state: ConversationState) -> DecisionSignals:
    """Read cached Phase-2 signals from slots — never recompute trust/risk/persona/emotion."""
    slots = state.slots
    persona = slots.get("persona") or {}
    ability = str(persona.get("ability") or "medium")
    willingness = str(persona.get("willingness") or "medium")
    risk_raw = slots.get("risk_flags") or []
    risk_flag_names: list[str] = []
    if isinstance(risk_raw, list):
        for item in risk_raw:
            if isinstance(item, dict):
                risk_flag_names.append(str(item.get("flag", "")))
            else:
                risk_flag_names.append(str(item))
    recovery = slots.get("recovery") or {}
    p_cure = float(recovery.get("p_cure", 0.5)) if isinstance(recovery, dict) else 0.5
    expected_pv = (
        float(recovery.get("expected_recovery_pv", 0.0)) if isinstance(recovery, dict) else 0.0
    )
    return DecisionSignals(
        trust=int(slots.get("trust") or 50),
        bucket=str(slots.get("bucket") or "standard"),
        ability=ability,
        willingness=willingness,
        primary_persona=persona.get("primary_persona"),
        emotion=str(slots.get("emotion") or "neutral"),
        emotion_intensity=str(slots.get("emotion_intensity") or "med"),
        risk_flags=[flag for flag in risk_flag_names if flag],
        p_cure=p_cure,
        expected_recovery_pv=expected_pv,
    )


def ability_willingness_quadrant(signals: DecisionSignals) -> str:
    ability = signals.ability
    willingness = signals.willingness
    ability_high = ability in ("high", "medium")
    willingness_high = willingness in ("high", "medium")
    if ability_high and willingness_high:
        return "CAN_WILL"
    if not ability_high and willingness_high:
        return "WANTS_CANT"
    if ability_high and not willingness_high:
        return "CAN_WONT"
    return "CANT_WONT"


def ptp_max_days_for_trust(trust: int) -> int:
    """High trust → longer in-policy PTP window; low trust → shorter leash."""
    if trust >= 75:
        return 21
    if trust >= 45:
        return 14
    return 7


def _pressure_allowed(quadrant: str) -> bool:
    return quadrant not in ("WANTS_CANT",)


def _branch_targets(branches: list[FlowBranch]) -> list[str]:
    targets: list[str] = []
    for branch in branches:
        if branch.then:
            targets.append(branch.then)
        if branch.else_:
            targets.append(branch.else_)
    return targets


def _step_candidates(step: FlowStep) -> list[str]:
    candidates: list[str] = []
    if step.action:
        candidates.append(step.action)
    if step.utter:
        candidates.append(step.utter)
    if step.collect:
        candidates.append(f"collect:{step.collect}")
    if isinstance(step.next, list):
        candidates.extend(_branch_targets(step.next))
    if step.decide:
        candidates.extend(_branch_targets(step.decide))
    return candidates


def enumerate_candidates(state: ConversationState, flows: FlowSet) -> list[DecisionCandidate]:
    """List allowable next actions from the active executor frame — does not invent actions."""
    if not state.flow_stack:
        return []

    frame = state.flow_stack[-1]
    if frame.parked:
        return []

    flow = flows.flows.get(frame.flow)
    if flow is None or frame.step_index >= len(flow.steps):
        return []

    step = flow.steps[frame.step_index]
    raw_ids = _step_candidates(step)

    synthetic = (
        "offer_partial_payment",
        "forward_ptp_empathy",
        "behavioral_risk_watch",
        "settlement_review",
        "restructure_review",
        "diagnose_hardship",
    )
    raw_ids.extend(synthetic)

    seen: set[str] = set()
    candidates: list[DecisionCandidate] = []
    for action_id in raw_ids:
        if action_id in seen:
            continue
        seen.add(action_id)
        category = ACTION_CATEGORY.get(action_id, "standard")
        candidates.append(
            DecisionCandidate(
                action_id=action_id,
                category=category,
                human_owned=action_id in HUMAN_OWNED_ACTIONS,
                recovery_value=CATEGORY_RECOVERY.get(category, 0.4),
                contact_cost=CATEGORY_CONTACT_COST.get(category, 0.5),
                experience_cost=CATEGORY_EXPERIENCE_COST.get(category, 0.5),
            )
        )
    return candidates


def _compliance_blocked(candidate: DecisionCandidate, quadrant: str) -> bool:
    if candidate.action_id in AGGRESSIVE_PRESSURE_ACTIONS and not _pressure_allowed(quadrant):
        return True
    if candidate.category == "aggressive_pressure" and quadrant == "WANTS_CANT":
        return True
    return False


def score_candidate(
    candidate: DecisionCandidate,
    signals: DecisionSignals,
    quadrant: str,
    *,
    recovery_boost: float = 0.0,
) -> float:
    """maximize E[recovery] − λ1·contact − λ2·compliance − λ3·experience (λ2 = ∞)."""
    if _compliance_blocked(candidate, quadrant):
        return float("-inf")

    preferred = QUADRANT_PREFERRED.get(quadrant, ())
    category = candidate.category
    recovery = candidate.recovery_value + recovery_boost
    if category in preferred:
        recovery += 0.25
    if quadrant == "CAN_WONT" and category in ("partial_ptp", "empathy_partial"):
        recovery -= 0.35
    if quadrant == "WANTS_CANT" and category == "aggressive_pressure":
        return float("-inf")
    if "strategic_default" in signals.risk_flags and category in ("partial_ptp", "empathy_partial"):
        recovery -= 0.2

    contact = candidate.contact_cost
    experience = candidate.experience_cost
    if signals.emotion in ("fear", "shame", "hopelessness") and category == "aggressive_pressure":
        return float("-inf")

    return recovery - (LAMBDA_CONTACT * contact) - (LAMBDA_EXPERIENCE * experience)


def rank_candidates(
    candidates: list[DecisionCandidate],
    signals: DecisionSignals,
    quadrant: str,
    *,
    recovery_boost: float = 0.0,
) -> list[DecisionCandidate]:
    scored = [
        (score_candidate(candidate, signals, quadrant, recovery_boost=recovery_boost), candidate)
        for candidate in candidates
    ]
    scored.sort(key=lambda item: (-item[0], item[1].action_id))
    return [candidate for score, candidate in scored if score > float("-inf")]


def compute_overlay(state: ConversationState, flows: FlowSet) -> DecisionOverlayResult:
    signals = extract_signals(state)
    if hardship_context_active(state):
        signals = signals.model_copy(update={"ability": "low", "willingness": "high"})
    quadrant = ability_willingness_quadrant(signals)
    candidates = enumerate_candidates(state, flows)
    boost = recovery_effort_boost(state)
    ranked = rank_candidates(candidates, signals, quadrant, recovery_boost=boost)

    executable = [candidate for candidate in ranked if not candidate.human_owned]
    human_recs = [
        candidate.action_id
        for candidate in ranked
        if candidate.human_owned or candidate.category.startswith("human_")
    ]

    selected = executable[0].action_id if executable else None
    top_score = (
        score_candidate(executable[0], signals, quadrant, recovery_boost=boost)
        if executable
        else 0.0
    )

    strategy = "standard"
    if quadrant == "WANTS_CANT":
        strategy = "empathy_partial"
    elif quadrant == "CAN_WONT":
        strategy = "firm_factual"
    elif quadrant == "CAN_WILL":
        strategy = "frictionless"
    elif quadrant == "CANT_WONT":
        strategy = "diagnose_escalate"

    return DecisionOverlayResult(
        quadrant=quadrant,
        selected_action=selected,
        ranked_actions=[candidate.action_id for candidate in ranked],
        human_recommendations=sorted(set(human_recs)),
        ptp_max_days=ptp_max_days_for_trust(signals.trust),
        pressure_allowed=_pressure_allowed(quadrant),
        decision_strategy=strategy,
        objective_score=round(top_score, 4),
        source="rules",
    )


def apply_decision_overlay(
    state: ConversationState,
    flows: FlowSet,
) -> ConversationState:
    """Apply overlay policy slots before executor walk — no LLM, no gate bypass."""
    overlay = compute_overlay(state, flows)
    updated = state.model_copy(deep=True)
    slots = dict(updated.slots)
    slots["decision_quadrant"] = overlay.quadrant
    slots["decision_strategy"] = overlay.decision_strategy
    slots["ptp_max_days"] = overlay.ptp_max_days
    slots["pressure_allowed"] = overlay.pressure_allowed
    slots["overlay_selected_action"] = overlay.selected_action
    slots["overlay_ranked_actions"] = overlay.ranked_actions
    slots["overlay_human_recommendations"] = overlay.human_recommendations
    slots["overlay_objective_score"] = overlay.objective_score
    slots["decision_overlay"] = overlay.model_dump(mode="json")

    updated.slots = slots
    updated.events.append(
        Event(
            ts=datetime.now(UTC).isoformat(),
            kind="decision_overlay",
            data=overlay.model_dump(mode="json"),
        )
    )
    return updated
