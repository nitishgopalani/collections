"""4-level compliance gate (Sprint 6) — final word on every outbound line."""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import TenantConfig
from app.engine.compliance_handoff import dunning_suppressed, reply_discloses_debt_or_arrears
from app.engine.compliance_rules import (
    flags,
    is_collection_pressure,
    matches_any,
    within_call_window,
)
from app.schemas.compliance import ComplianceLevel, GateResult
from app.schemas.state import ConversationState


def gate(
    reply_text: str,
    state: ConversationState,
    tenant_cfg: TenantConfig,
    *,
    inbound_transcript: str | None = None,
    now: datetime | None = None,
) -> GateResult:
    """Inspect every outbound line; nothing bypasses this gate."""
    state_flags = flags(state)
    clock = now or datetime.now(tz=ZoneInfo(tenant_cfg.call_window_timezone))
    inbound = inbound_transcript or ""

    critical_inbound = matches_any(inbound, tenant_cfg.critical_inbound_phrases)
    if critical_inbound:
        return GateResult(
            verdict="block",
            text=tenant_cfg.safe_fallback_reply,
            level="CRITICAL",
            reason=f"critical_inbound:{critical_inbound}",
            transfer_to_human=True,
        )

    if state_flags.get("vulnerable") or state.slots.get("vulnerable_routed"):
        if is_collection_pressure(reply_text, tenant_cfg):
            return GateResult(
                verdict="block",
                text=tenant_cfg.care_first_reply,
                level="CRITICAL",
                reason="vulnerable_no_dunning",
                transfer_to_human=True,
            )

    if state_flags.get("opt_out") and not state.slots.get("opt_out_ack_this_turn"):
        return GateResult(
            verdict="block",
            text=tenant_cfg.silent_reply,
            level="HIGH",
            reason="opt_out_active",
        )

    if reply_discloses_debt_or_arrears(reply_text, state):
        reason = (
            "third_party_debt_disclosure"
            if state.slots.get("third_party_active") or state.slots.get("confirmed_not_borrower")
            else "pre_verification_debt_disclosure"
        )
        return GateResult(
            verdict="block",
            text=tenant_cfg.safe_fallback_reply,
            level="CRITICAL",
            reason=reason,
            transfer_to_human=True,
        )

    if dunning_suppressed(state) and is_collection_pressure(reply_text, tenant_cfg):
        return GateResult(
            verdict="block",
            text=tenant_cfg.safe_fallback_reply,
            level="CRITICAL",
            reason="dunning_suppressed",
            transfer_to_human=bool(state.slots.get("transfer_to_human")),
        )

    if not within_call_window(tenant_cfg, clock):
        return GateResult(
            verdict="block",
            text=tenant_cfg.silent_reply,
            level="HIGH",
            reason="outside_call_window",
        )

    attempts_today = int(state_flags.get("attempts_today", state.attempts))
    if attempts_today > tenant_cfg.max_attempts_per_day:
        return GateResult(
            verdict="block",
            text=tenant_cfg.silent_reply,
            level="HIGH",
            reason="attempt_cap_daily",
        )

    dispute_active = (
        state_flags.get("dispute_hold")
        or state_flags.get("dispute_logged")
        or state.slots.get("dispute_logged")
    )
    if dispute_active and is_collection_pressure(reply_text, tenant_cfg):
        return GateResult(
            verdict="block",
            text=tenant_cfg.safe_fallback_reply,
            level="MEDIUM",
            reason="dispute_hold_no_pressure",
        )

    prohibited = matches_any(reply_text, tenant_cfg.prohibited_outbound_phrases)
    if prohibited:
        return GateResult(
            verdict="modify",
            text=tenant_cfg.safe_fallback_reply,
            level="CRITICAL",
            reason=f"prohibited_language:{prohibited}",
            transfer_to_human=True,
        )

    level: ComplianceLevel = "LOW"
    if state_flags.get("dispute_hold"):
        level = "MEDIUM"
    return GateResult(
        verdict="allow",
        text=reply_text,
        level=level,
        reason="ok",
    )
