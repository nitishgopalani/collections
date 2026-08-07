"""4-level compliance gate (Sprint 6) — final word on every outbound line."""

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import TenantConfig
from app.engine.compliance_handoff import dunning_suppressed, reply_discloses_debt_or_arrears
from app.engine.compliance_rules import (
    evaluate_pressure_with_allowlist,
    flags,
    matches_any,
    within_call_window,
)
from app.engine.tenant_profile import get_tenant_profile
from app.schemas.compliance import ComplianceLevel, GateResult
from app.schemas.state import ConversationState

logger = logging.getLogger(__name__)


def _allowlisted_phrases(tenant_cfg: TenantConfig) -> list[str]:
    profile = get_tenant_profile(tenant_cfg.tenant_id)
    if profile is None:
        return []
    return list(profile.gate_allowlisted_phrases or [])


def _pressure_decision(
    reply_text: str,
    tenant_cfg: TenantConfig,
) -> tuple[bool, list[dict]]:
    """Return ``(should_block, warnings)`` for collection-pressure language."""
    blocking, warnings = evaluate_pressure_with_allowlist(
        reply_text,
        tenant_cfg.collection_pressure_phrases,
        _allowlisted_phrases(tenant_cfg),
    )
    if warnings:
        logger.info(
            "gate_warnings %s",
            json.dumps(
                {"tenant_id": tenant_cfg.tenant_id, "warnings": warnings},
                ensure_ascii=False,
            ),
        )
    return blocking is not None, warnings


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
    accumulated_warnings: list[dict] = []

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
        should_block, warnings = _pressure_decision(reply_text, tenant_cfg)
        accumulated_warnings.extend(warnings)
        if should_block:
            return GateResult(
                verdict="block",
                text=tenant_cfg.care_first_reply,
                level="CRITICAL",
                reason="vulnerable_no_dunning",
                transfer_to_human=True,
                warnings=accumulated_warnings,
            )

    if state_flags.get("opt_out") and not state.slots.get("opt_out_ack_this_turn"):
        return GateResult(
            verdict="block",
            text=tenant_cfg.silent_reply,
            level="HIGH",
            reason="opt_out_active",
            warnings=accumulated_warnings,
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
            warnings=accumulated_warnings,
        )

    if tenant_cfg.enforce_compliance_gate:
        if dunning_suppressed(state):
            should_block, warnings = _pressure_decision(reply_text, tenant_cfg)
            accumulated_warnings.extend(warnings)
            if should_block:
                return GateResult(
                    verdict="block",
                    text=tenant_cfg.safe_fallback_reply,
                    level="CRITICAL",
                    reason="dunning_suppressed",
                    transfer_to_human=bool(state.slots.get("transfer_to_human")),
                    warnings=accumulated_warnings,
                )

    if not within_call_window(tenant_cfg, clock):
        return GateResult(
            verdict="block",
            text=tenant_cfg.silent_reply,
            level="HIGH",
            reason="outside_call_window",
            warnings=accumulated_warnings,
        )

    attempts_today = int(state_flags.get("attempts_today", state.attempts))
    if attempts_today > tenant_cfg.max_attempts_per_day:
        return GateResult(
            verdict="block",
            text=tenant_cfg.silent_reply,
            level="HIGH",
            reason="attempt_cap_daily",
            warnings=accumulated_warnings,
        )

    if tenant_cfg.enforce_compliance_gate:
        dispute_active = (
            state_flags.get("dispute_hold")
            or state_flags.get("dispute_logged")
            or state.slots.get("dispute_logged")
        )
        if dispute_active:
            should_block, warnings = _pressure_decision(reply_text, tenant_cfg)
            accumulated_warnings.extend(warnings)
            if should_block:
                return GateResult(
                    verdict="block",
                    text=tenant_cfg.safe_fallback_reply,
                    level="MEDIUM",
                    reason="dispute_hold_no_pressure",
                    warnings=accumulated_warnings,
                )

        prohibited = matches_any(reply_text, tenant_cfg.prohibited_outbound_phrases)
        if prohibited:
            return GateResult(
                verdict="modify",
                text=tenant_cfg.safe_fallback_reply,
                level="CRITICAL",
                reason=f"prohibited_language:{prohibited}",
                transfer_to_human=True,
                warnings=accumulated_warnings,
            )

    level: ComplianceLevel = "LOW"
    if state_flags.get("dispute_hold"):
        level = "MEDIUM"
    return GateResult(
        verdict="allow",
        text=reply_text,
        level=level,
        reason="ok",
        warnings=accumulated_warnings,
    )
