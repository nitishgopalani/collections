"""Real-time safety / vulnerability pre-empt (Sprint 6)."""

from app.config import TenantConfig
from app.engine.compliance_rules import flags, matches_any
from app.schemas.compliance import SafetyResult
from app.schemas.state import ConversationState


def safety_preempt(
    text: str,
    state: ConversationState,
    tenant_cfg: TenantConfig,
    *,
    emotion_label: str | None = None,
    emotion_intensity: str | None = None,
) -> SafetyResult | None:
    """High-recall distress detector — false negative is unacceptable."""
    state_flags = flags(state)
    if state_flags.get("vulnerable"):
        return SafetyResult(
            reason="existing_vulnerable_flag",
            reply_text=tenant_cfg.care_first_reply,
            compliance_updates={"vulnerable": True, "recovery_suspended": True},
        )

    if emotion_label == "hopelessness" and emotion_intensity == "high":
        return SafetyResult(
            reason="emotion_hopelessness_high",
            reply_text=tenant_cfg.care_first_reply,
            transfer_to_human=True,
            suspend_recovery=True,
            compliance_updates={
                "vulnerable": True,
                "recovery_suspended": True,
                "dunning_suppressed": True,
            },
        )

    vuln_hit = matches_any(text, tenant_cfg.vulnerability_signals)
    distress_hit = matches_any(text, tenant_cfg.distress_signals)
    if vuln_hit is None and distress_hit is None:
        return None

    reason = f"vulnerability_signal:{vuln_hit or distress_hit}"
    return SafetyResult(
        reason=reason,
        reply_text=tenant_cfg.care_first_reply,
        transfer_to_human=True,
        suspend_recovery=True,
        compliance_updates={
            "vulnerable": True,
            "recovery_suspended": True,
            "dunning_suppressed": True,
        },
    )


def apply_safety_to_state(state: ConversationState, result: SafetyResult) -> ConversationState:
    updated = state.model_copy(deep=True)
    state_flags = flags(updated)
    state_flags.update(result.compliance_updates)
    slots = dict(updated.slots)
    slots["compliance_flags"] = state_flags
    slots["transfer_to_human"] = result.transfer_to_human
    if result.suspend_recovery:
        slots["recovery_suspended"] = True
        slots["dunning_suppressed"] = True
    updated.slots = slots
    return updated
