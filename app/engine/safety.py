"""Real-time safety / vulnerability pre-empt (Sprint 6)."""

from datetime import datetime
from typing import Any

from zoneinfo import ZoneInfo

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
    profile: Any | None = None,
) -> SafetyResult | None:
    """High-recall distress detector — false negative is unacceptable."""
    _close_reply = (
        getattr(profile, "vulnerability_close", "") or ""
    ) if profile is not None else ""
    _fallback = _close_reply or tenant_cfg.care_first_reply
    state_flags = flags(state)
    if state_flags.get("vulnerable"):
        return SafetyResult(
            reason="existing_vulnerable_flag",
            reply_text=_fallback,
            compliance_updates={"vulnerable": True, "recovery_suspended": True},
        )

    if emotion_label == "hopelessness" and emotion_intensity == "high":
        return SafetyResult(
            reason="emotion_hopelessness_high",
            reply_text=_fallback,
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
        reply_text=_fallback,
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


def dnc_preempt(
    text: str,
    state: ConversationState,
    tenant_cfg: TenantConfig,
    *,
    profile: Any | None = None,
) -> SafetyResult | None:
    """W1-C C2 (DNC/opt-out capture, policy interrupt): high-recall DNC detector.

    Fires BEFORE the Tier-1 evidence scorer on cues by which the caller asks us
    to stop calling (``dobara call mat karna`` / ``call mat karo`` /
    ``pareshan mat karo`` …). Preempts the turn, speaks the non-committal
    ``policy_stop_calls`` wording (request recorded; final confirmation from
    the brand — does NOT promise dialer suppression until W4 dialer work),
    tags ``disposition=dnc_requested`` and graceful END (outcome 7).

    Distinct from ``apply_opt_out`` (an action the scorer emits) — this is the
    policy-lane preempt that never lets the scorer run on a DNC cue.
    """
    hit = matches_any(text, tenant_cfg.dnc_signals)
    if hit is None:
        return None
    _ack = (
        getattr(profile, "dnc_ack", "") or ""
    ) if profile is not None else ""
    return SafetyResult(
        reason=f"dnc_signal:{hit}",
        reply_text=_ack or tenant_cfg.policy_stop_calls_reply,
        end_call=True,
        compliance_updates={
            "dnc_requested": True,
            # Do NOT set dunning_suppressed here — that is the W4 dialer-suppression
            # action and would be a false promise until the dialer honors the flag.
            # The audit flag (dnc_requested) is enough for this release.
        },
    )


def apply_dnc_to_state(state: ConversationState, result: SafetyResult) -> ConversationState:
    updated = state.model_copy(deep=True)
    state_flags = flags(updated)
    state_flags.update(result.compliance_updates)
    slots = dict(updated.slots)
    slots["compliance_flags"] = state_flags
    slots["disposition"] = "dnc_requested"
    # Mark the call closed so the terminal guard fires on a late barge-in
    # (graceful END — the call does not continue after a DNC).
    if result.end_call:
        slots["end_call"] = True
    updated.slots = slots
    return updated


def call_window_preempt(
    state: ConversationState,
    tenant_cfg: TenantConfig,
    *,
    now: datetime | None = None,
    profile: Any | None = None,
) -> SafetyResult | None:
    """W1-C C3 (call-window close-out, policy interrupt): mid-call window close.

    Fires BEFORE the Tier-1 evidence scorer when an ANSWERED call has crossed
    the configured call-window boundary mid-conversation. Speaks the scripted
    polite close (``call_window_close_reply``), tags
    ``disposition=call_window_closed`` and graceful ENDs (outcome 7) — never
    a mid-call ``silent_reply``.

    Only fires mid-call (``state.attempts > 0``). On the first turn (call
    initiation outside the window) the gate's silent ``outside_call_window``
    block is correct — we simply do not answer. Mid-call we never go silent.
    """
    from app.engine.compliance_rules import within_call_window

    if state.attempts < 1:
        return None
    clock = now or datetime.now(tz=ZoneInfo(tenant_cfg.call_window_timezone))
    if within_call_window(tenant_cfg, clock):
        return None
    _close = (
        getattr(profile, "window_close", "") or ""
    ) if profile is not None else ""
    return SafetyResult(
        reason="call_window_crossed_mid_call",
        reply_text=_close or tenant_cfg.call_window_close_reply,
        transfer_to_human=False,
        suspend_recovery=False,
        end_call=True,
        compliance_updates={
            "call_window_closed": True,
        },
    )


def apply_call_window_to_state(state: ConversationState, result: SafetyResult) -> ConversationState:
    updated = state.model_copy(deep=True)
    state_flags = flags(updated)
    state_flags.update(result.compliance_updates)
    slots = dict(updated.slots)
    slots["compliance_flags"] = state_flags
    slots["disposition"] = "call_window_closed"
    if result.end_call:
        slots["end_call"] = True
    updated.slots = slots
    return updated


def third_party_flip_preempt(
    text: str,
    state: ConversationState,
    tenant_cfg: TenantConfig,
    *,
    profile: Any | None = None,
) -> SafetyResult | None:
    """W1-C C4 (third-party / speaker-flip guard, policy interrupt).

    Fires BEFORE the Tier-1 evidence scorer on mid-call cues by which a
    different speaker joins or takes over the call (``main uski/uska X bol
    raha/rahi``, ``wo bahar hai, main…``, ``main ramesh ka bhai bol raha hoon``
    …). Revokes ``identity_current`` (identity_ok=False), locks disclosure
    (strict) or downgrades to generic-only facts (relaxed), speaks the
    third-party script + callback capture, and tags
    ``disposition=THIRD_PARTY_FLAGGED``.

    DPDP posture is BRAND-CONFIGURABLE (W1-C amendment):
      strict  = disclosure LOCK → third-party script → callback → END (outcome 7).
      relaxed = generic-only facts (no amounts/dates/PII); conversation may continue.
      dpdp_disclosure_tier_enforced=false = open-tier (lab use); facts flow freely.

    ALWAYS-ON regardless of mode: ``third_party_suspected=true``,
    ``identity_current`` transition logged, disposition tagged. The audit
    trail is not configurable — only the enforcement is.
    """
    hit = matches_any(text, tenant_cfg.third_party_flip_signals)
    if hit is None:
        return None

    # Resolve DPDP posture from the TenantRuntimeProfile (brand-configurable).
    lock = "strict"
    enforced = True
    if profile is not None:
        lock = (getattr(profile, "dpdp_third_party_lock", "strict") or "strict").lower().strip()
        enforced = bool(getattr(profile, "dpdp_disclosure_tier_enforced", True))

    if not enforced:
        # Open-tier (lab use): log the suspicion but do not lock or end.
        return SafetyResult(
            reason=f"third_party_flip:{hit}:open_tier",
            reply_text="",
            transfer_to_human=False,
            suspend_recovery=False,
            end_call=False,
            compliance_updates={
                "third_party_suspected": True,
                "third_party_open_tier": True,
            },
        )

    if lock == "relaxed":
        _tp_close = (
            getattr(profile, "third_party_close", "") or ""
        ) if profile is not None else ""
        return SafetyResult(
            reason=f"third_party_flip:{hit}:relaxed",
            reply_text=_tp_close or tenant_cfg.third_party_flip_reply_relaxed,
            transfer_to_human=False,
            suspend_recovery=False,
            end_call=False,
            compliance_updates={
                "third_party_suspected": True,
                "third_party_active": True,
                "identity_ok": False,
                "dpdp_mode": "relaxed",
            },
        )

    # strict (default)
    _tp_close = (
        getattr(profile, "third_party_close", "") or ""
    ) if profile is not None else ""
    return SafetyResult(
        reason=f"third_party_flip:{hit}:strict",
        reply_text=_tp_close or tenant_cfg.third_party_flip_reply_strict,
        transfer_to_human=False,
        suspend_recovery=False,
        end_call=True,
        compliance_updates={
            "third_party_suspected": True,
            "third_party_active": True,
            "identity_ok": False,
            "dpdp_mode": "strict",
        },
    )


def apply_third_party_flip_to_state(
    state: ConversationState,
    result: SafetyResult,
) -> ConversationState:
    """Apply the third-party flip to state: revoke identity_current, lock
    disclosure (third_party_active), tag disposition, and ALWAYS log the
    third_party_suspected + identity_current transition (audit trail)."""
    updated = state.model_copy(deep=True)
    state_flags = flags(updated)
    state_flags.update(result.compliance_updates)
    slots = dict(updated.slots)
    slots["compliance_flags"] = state_flags
    # Revoke identity_current — the prior identity assertion no longer holds.
    slots["identity_ok"] = False
    # Disclosure LOCK: third_party_active makes must_block_debt_disclosure True
    # → slots_for_nlg strips DEBT_SLOT_KEYS (amounts/dates/PII).
    if result.compliance_updates.get("third_party_active"):
        slots["third_party_active"] = True
    slots["disposition"] = "THIRD_PARTY_FLAGGED"
    if result.end_call:
        slots["end_call"] = True
    updated.slots = slots
    return updated
