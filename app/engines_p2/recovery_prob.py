"""Recovery Probability Engine (Sprint 13 / blueprint Engine 6).

Heuristic p_cure and expected recovery PV from cached Phase-2 signals and payment
history. Recovery informs prioritization, effort allocation, and dashboard — it is
NOT a license to bypass the compliance gate or auto-execute human-owned actions.

ML upgrade path (DATA-GATED)
----------------------------
The turn audit / event log already captures the feature vector each turn:
  - bucket, dpd, amount_due, trust_current, persona_current, risk_flags,
    emotion history, payment/ptp/broken_ptp events, overlay decision, gate verdict.

DECISION NEEDED: production label definition before training:
  - Label: ``cured_within_N_days`` (y/n) where N is agreed with product/risk
    (default proposal: N=90 from first contact in bucket).
  - Features to log for training (already in audit + borrower record):
    bucket, trust, persona primary/secondary, active risk_flags, ptp history,
    partial payment count, broken_ptp count, emotion at contact, decision_quadrant.
  - Swap point: when a calibrated model beats heuristic_v1 on hold-out AUC and
    passes fairness review, replace ``compute_heuristic_recovery`` with
    ``compute_ml_recovery`` behind the same ``RecoveryScore`` interface; set
    ``method="ml_v1"``. No orchestrator changes required — only this module.
  - Training data source: append-only audit stream + borrower outcome backfill.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.schemas.recovery import RecoveryScore
from app.schemas.state import BorrowerRecord, ConversationState

# Recovery informs effort; it never authorizes gate-prohibited conduct.
RECOVERY_IS_INPUT_NOT_LICENSE = True

METHOD_HEURISTIC_V1 = "heuristic_v1"
METHOD_ML_V1 = "ml_v1"  # reserved — activated when data-gated model ships

BUCKET_BASE_P_CURE: dict[str, float] = {
    "current": 0.62,
    "0-30": 0.52,
    "30-60": 0.42,
    "60-90": 0.28,
    "90+": 0.15,
    "B0": 0.62,
    "B1": 0.52,
    "B2": 0.42,
}

COOPERATIVE_PERSONAS: frozenset[str] = frozenset(
    {
        "genuine_payer",
        "salary_dependent",
        "forgetful",
        "genuine_settlement_candidate",
        "temporary_hardship",
    }
)

HIGH_RISK_PERSONAS: frozenset[str] = frozenset(
    {
        "strategic_defaulter",
        "promise_breaker",
        "chronic_tomorrow",
        "ghost",
        "settlement_hunter",
    }
)

RISK_PENALTIES: dict[str, float] = {
    "promise_breaking": 0.10,
    "strategic_default": 0.12,
    "excuse_recycling": 0.06,
    "ghosting": 0.08,
    "settlement_fishing": 0.04,
    "fraud_indicator": 0.15,
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _active_risk_flags(borrower: BorrowerRecord, state: ConversationState | None) -> list[str]:
    if state is not None:
        raw = state.slots.get("risk_flags") or []
        flags: list[str] = []
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    flags.append(str(item.get("flag", "")))
                else:
                    flags.append(str(item))
        if flags:
            return [flag for flag in flags if flag]
    return [
        str(item.get("flag", ""))
        for item in borrower.risk_flags
        if float(item.get("confidence", 0.0)) >= 0.5 and item.get("flag")
    ]


def _payment_history_adjustment(borrower: BorrowerRecord) -> float:
    kept = sum(1 for ptp in borrower.ptps if str(ptp.get("status", "")).lower() == "kept")
    broken = len(borrower.broken_ptps) + sum(
        1 for ptp in borrower.ptps if str(ptp.get("status", "")).lower() == "broken"
    )
    partials = sum(1 for payment in borrower.payments if payment.get("partial"))
    kept_bonus = min(0.08, kept * 0.025)
    broken_penalty = min(0.14, broken * 0.035)
    partial_bonus = min(0.04, partials * 0.015)
    return kept_bonus + partial_bonus - broken_penalty


def _read_amount_due(borrower: BorrowerRecord, state: ConversationState | None) -> float:
    if state is not None:
        raw = state.slots.get("amount_due")
        if raw is not None:
            return max(0.0, float(raw))
    loan = borrower.loan
    for key in ("amount_due", "outstanding"):
        if key in loan and loan[key] is not None:
            return max(0.0, float(loan[key]))
    return 0.0


def _read_bucket(borrower: BorrowerRecord, state: ConversationState | None) -> str:
    if state is not None and state.slots.get("bucket") is not None:
        return str(state.slots["bucket"])
    return str(borrower.loan.get("bucket") or "30-60")


def _read_trust(borrower: BorrowerRecord, state: ConversationState | None) -> int:
    if state is not None and state.slots.get("trust") is not None:
        return int(state.slots["trust"])
    return int(borrower.trust_current)


def _read_persona_primary(borrower: BorrowerRecord, state: ConversationState | None) -> str:
    if state is not None:
        persona = state.slots.get("persona") or {}
        if isinstance(persona, dict) and persona.get("primary_persona"):
            return str(persona["primary_persona"])
    return str((borrower.persona_current or {}).get("primary_persona") or "")


def compute_heuristic_recovery(
    borrower: BorrowerRecord,
    *,
    state: ConversationState | None = None,
    ts: datetime | None = None,
) -> RecoveryScore:
    """Transparent heuristic — base rate by bucket, adjusted by trust/persona/risk/history."""
    stamp = (ts or datetime.now(UTC)).isoformat()
    bucket = _read_bucket(borrower, state)
    trust = _read_trust(borrower, state)
    primary_persona = _read_persona_primary(borrower, state)
    risk_flags = _active_risk_flags(borrower, state)

    base = BUCKET_BASE_P_CURE.get(bucket, 0.40)
    trust_adj = (trust - 50) / 200.0

    persona_adj = 0.0
    if primary_persona in COOPERATIVE_PERSONAS:
        persona_adj = 0.08
    elif primary_persona in HIGH_RISK_PERSONAS:
        persona_adj = -0.10

    history_adj = _payment_history_adjustment(borrower)
    risk_penalty = sum(RISK_PENALTIES.get(flag, 0.05) for flag in risk_flags)

    p_cure = _clamp(base + trust_adj + persona_adj + history_adj - risk_penalty, 0.0, 1.0)
    amount_due = _read_amount_due(borrower, state)
    expected_pv = round(amount_due * p_cure, 2)

    return RecoveryScore(
        p_cure=round(p_cure, 4),
        expected_recovery_pv=expected_pv,
        last_scored=stamp,
        method=METHOD_HEURISTIC_V1,
        explain={
            "bucket": bucket,
            "base_rate": round(base, 4),
            "trust_adj": round(trust_adj, 4),
            "persona_adj": round(persona_adj, 4),
            "history_adj": round(history_adj, 4),
            "risk_penalty": round(risk_penalty, 4),
            "primary_persona": primary_persona or "unknown",
        },
    )


def recovery_to_slot(score: RecoveryScore) -> dict[str, Any]:
    return score.model_dump(mode="json")


def refresh_borrower_recovery(
    borrower: BorrowerRecord,
    *,
    state: ConversationState | None = None,
    ts: datetime | None = None,
) -> BorrowerRecord:
    score = compute_heuristic_recovery(borrower, state=state, ts=ts)
    updated = borrower.model_copy(deep=True)
    updated.recovery = recovery_to_slot(score)
    return updated


def sync_recovery_on_persist(
    borrower: BorrowerRecord,
    *,
    state: ConversationState | None = None,
    trigger: str = "turn_persist",
) -> BorrowerRecord:
    """Recompute recovery on persist — off hot path, uses post-sync borrower signals."""
    updated = refresh_borrower_recovery(borrower, state=state)
    recovery = dict(updated.recovery)
    recovery["trigger"] = trigger
    updated.recovery = recovery
    return updated


def apply_recovery_to_state(
    state: ConversationState,
    borrower: BorrowerRecord,
) -> ConversationState:
    """Expose cached recovery on live call slots for overlay/dashboard reads."""
    if not borrower.recovery:
        return state
    updated = state.model_copy(deep=True)
    slots = dict(updated.slots)
    slots["recovery"] = dict(borrower.recovery)
    updated.slots = slots
    return updated


def recovery_effort_boost(state: ConversationState) -> float:
    """Small ranking boost for overlay — higher expected PV raises compliant effort priority."""
    recovery = state.slots.get("recovery") or {}
    if not isinstance(recovery, dict):
        return 0.0
    p_cure = float(recovery.get("p_cure", 0.5))
    expected_pv = float(recovery.get("expected_recovery_pv", 0.0))
    pv_component = min(0.08, expected_pv / 100_000.0)
    return min(0.15, p_cure * 0.10 + pv_component)
