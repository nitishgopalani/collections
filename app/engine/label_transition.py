"""Label Transition Layer (LTL) — Phase 1, deterministic/rules-only.

Runs inside ``handle_turn`` after all command shaping (LLM command_gen, SOT
coercions, slot validation, confidence floor, dispute accumulator) and BEFORE
``tracker.apply``. It answers ``previous_label + current_label + state -> decision``
and, only in enforce mode for a supported provider, rewrites the existing command
primitives (``start_flow`` / ``cancel_flow`` / ``set_slot`` / ``clarify``). It never
mutates ``flow_stack`` directly.

Two modes:
  * shadow  — detect labels + record state/logs; commands are returned unchanged.
  * enforce — may rewrite commands, but only for a provider that supports enforce
              (Phase 1: salary_on_time) and only using real, verified flows.

This module must not import ``turn.py`` (cycle). The small amount of cue logic it
needs is duplicated here intentionally (documented in docs/LTL_IMPLEMENTATION_QA.md).
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.schemas.command import Command
from app.schemas.flow import FlowSet
from app.schemas.label_transition import (
    HIGH_RISK_LABELS,
    MONEY_PATH_LABELS,
    BlockedTransition,
    Decision,
    Label,
    LabelHistoryItem,
    LabelStateModel,
    LabelTransitionProviderInfo,
    TransitionDecision,
    UnresolvedRisk,
    label_namespace,
)
from app.schemas.state import ConversationState, Event

LABEL_STATE_SLOT = "_label"
_MAX_HISTORY = 50

# --- Salary On Time label <-> real flow maps (verified from pre_closure.yml) ------
SOT_LABEL_TO_FLOW: dict[str, str] = {
    Label.SUPPORT_PAYMENT_LINK_REQUEST: "sot_obj_link_request",
    Label.SUPPORT_DIFF_NUMBER_LINK: "sot_obj_diff_number_link",
    Label.SUPPORT_NO_LINK_PREF: "sot_obj_no_link_pref",
    Label.DISPUTE_LOAN_NOT_TAKEN: "sot_obj_never_loan",
    Label.DISPUTE_WRONG_AMOUNT: "sot_obj_wrong_amount",
    Label.DISPUTE_ALREADY_PAID: "sot_obj_already_paid_q",
    Label.HARDSHIP_MEDICAL: "sot_obj_medical",
    Label.HARDSHIP_JOB_LOSS: "sot_obj_job_loss",
}
SOT_FLOW_TO_LABEL: dict[str, str] = {v: k for k, v in SOT_LABEL_TO_FLOW.items()}

# --- Conservative transcript cue sets (lowercased substring match) ---------------
_LINK_CUES = (
    "link", "पेमेंट लिंक", "लिंक", "payment link", "link bhej", "link do",
    "link chahiye", "send me the link", "bhej do link",
)
_OWNERSHIP_CONFIRM_CUES = (
    "ye mera loan hai", "mera loan hai", "mera hi loan", "loan mera hai",
    "yes this is my loan", "this loan is mine", "confirm this loan is mine",
    "galti se bola", "galti se bol", "haan mera loan", "ye mera hi loan",
    "मेरा लोन है", "मेरा ही लोन", "गलती से बोला",
)
_LOAN_NOT_TAKEN_CUES = (
    "loan liya hi nahi", "loan nahi liya", "maine loan nahi", "koi loan nahi",
    "i never took", "never took a loan", "no loan", "loan hi nahi liya",
    "लोन लिया ही नहीं", "लोन नहीं लिया", "कोई लोन नहीं",
)
_WRONG_AMOUNT_CUES = (
    "amount galat", "galat amount", "itna nahi", "itne paise nahi", "zyada bata",
    "amount wrong", "wrong amount", "amount zyada", "गलत अमाउंट", "इतना नहीं",
)
_ALREADY_PAID_CUES = (
    "already paid", "pehle hi kar diya", "payment kar diya", "bhugtan kar diya",
    "paisa de diya", "kar chuka", "पहले ही", "कर दिया पेमेंट",
)
_LEGAL_CUES = (
    "legal", "court", "kanoon", "kanooni", "vakil", "lawyer", "consumer court",
    "police", "complaint karunga", "कानून", "कोर्ट", "वकील",
)
_HARASSMENT_CUES = (
    "pareshan", "harass", "baar baar call", "tang kar", "shikayat karunga",
    "परेशान", "बार बार",
)
_SALARY_NOT_RECEIVED_CUES = (
    "salary nahi", "salary nahi aayi", "tankhwah nahi", "salary late",
    "salary abhi nahi", "सैलरी नहीं", "तनख्वाह नहीं",
)
_HARD_REFUSAL_CUES = (
    "nahi karunga", "nahi doonga", "will not pay", "won't pay", "pay nahi karunga",
    "kabhi nahi", "bilkul nahi karunga", "नहीं करूंगा", "नहीं दूंगा",
)
_SOFT_REFUSAL_CUES = (
    "abhi nahi", "baad me", "baad mein", "thodi der", "kuch din", "time chahiye",
    "abhi mushkil", "abhi possible nahi", "अभी नहीं", "बाद में",
)
_TODAY_CUES = ("aaj", "abhi", "today", "aaj hi", "abhi kar", "turant", "आज", "अभी")


def _matches(text: str, cues: tuple[str, ...]) -> bool:
    low = (text or "").strip().lower()
    return any(cue in low for cue in cues)


def has_ownership_confirmation(transcript: str) -> bool:
    return _matches(transcript, _OWNERSHIP_CONFIRM_CUES)


# --- Providers -------------------------------------------------------------------
def get_label_transition_provider(
    tenant_id: str, settings: object | None = None
) -> LabelTransitionProviderInfo:
    """Return the provider for this tenant.

    A scripted tenant with ``ltl_enforce_enabled=true`` gets the enforce adapter
    (real flow maps). Everything else → generic, shadow only (no flow maps).
    """
    from app.engine.tenant_profile import get_tenant_profile

    profile = get_tenant_profile(tenant_id)
    if profile is not None and profile.ltl_enforce_enabled:
        return LabelTransitionProviderInfo(
            name=profile.tenant_id,
            supports_shadow=True,
            supports_enforce=True,
            label_to_flow=dict(SOT_LABEL_TO_FLOW),
            flow_to_label=dict(SOT_FLOW_TO_LABEL),
        )
    return LabelTransitionProviderInfo(
        name="generic",
        supports_shadow=True,
        supports_enforce=False,
        unsupported_reason="no enforce adapter for this tenant",
    )


# --- State load/save -------------------------------------------------------------
def load_label_state(state: ConversationState) -> LabelStateModel:
    raw = state.slots.get(LABEL_STATE_SLOT)
    if not raw:
        return LabelStateModel()
    try:
        return LabelStateModel.model_validate(raw)
    except Exception:  # noqa: BLE001 — fail safe, never crash a turn
        return LabelStateModel()


def save_label_state(state: ConversationState, label_state: LabelStateModel) -> ConversationState:
    state.slots[LABEL_STATE_SLOT] = label_state.model_dump(mode="json")
    return state


# --- Detection (rules-only, Phase 1) ---------------------------------------------
def _label_from_slot(name: str | None, value: object, transcript: str) -> str | None:
    if not name:
        return None
    if name == "sot_payment_intent":
        if value == "willing":
            return (
                Label.PAYMENT_WILL_PAY_TODAY
                if _matches(transcript, _TODAY_CUES)
                else Label.PAYMENT_PROMISE_FUTURE_DATE
            )
        if value == "refused":
            return (
                Label.REFUSAL_SOFT
                if _matches(transcript, _SOFT_REFUSAL_CUES)
                else Label.REFUSAL_HARD
            )
    if name == "sot_identity_response":
        if value == "denied":
            return Label.IDENTITY_WRONG_PERSON
        if value == "confirmed":
            return Label.IDENTITY_CONFIRMED
    if name == "sot_knows_customer" and value in (False, "false", "no"):
        return Label.IDENTITY_WRONG_PERSON
    if name == "sot_link_received":
        # Still within the payment-link support context.
        return Label.SUPPORT_PAYMENT_LINK_REQUEST
    return None


def _label_from_transcript(transcript: str) -> str | None:
    # Order matters: high-risk / dispute cues take precedence over generic ones.
    if _matches(transcript, _LOAN_NOT_TAKEN_CUES):
        return Label.DISPUTE_LOAN_NOT_TAKEN
    if _matches(transcript, _ALREADY_PAID_CUES):
        return Label.DISPUTE_ALREADY_PAID
    if _matches(transcript, _WRONG_AMOUNT_CUES):
        return Label.DISPUTE_WRONG_AMOUNT
    if _matches(transcript, _LEGAL_CUES):
        return Label.RISK_LEGAL_THREAT
    if _matches(transcript, _HARASSMENT_CUES):
        return Label.RISK_HARASSMENT_COMPLAINT
    if _matches(transcript, _LINK_CUES):
        return Label.SUPPORT_PAYMENT_LINK_REQUEST
    if _matches(transcript, _SALARY_NOT_RECEIVED_CUES):
        return Label.HARDSHIP_SALARY_NOT_RECEIVED
    if _matches(transcript, _HARD_REFUSAL_CUES):
        return Label.REFUSAL_HARD
    if _matches(transcript, _SOFT_REFUSAL_CUES):
        return Label.REFUSAL_SOFT
    return None


def detect_current_labels(
    commands: list[Command],
    awaiting_slot: str,
    transcript: str,
    candidate_flows: object,
    provider: LabelTransitionProviderInfo,
) -> str | None:
    """Infer the current turn's label (rules-only). Signal priority: start_flow ->
    set_slot -> transcript cues. Returns None when nothing is confidently detected."""
    # A. explicit start_flow mapped by the provider
    for cmd in commands:
        if cmd.command == "start_flow" and cmd.flow:
            mapped = provider.flow_to_label.get(cmd.flow)
            if mapped:
                return mapped
    # B. set_slot signals (SOT slots)
    for cmd in commands:
        if cmd.command == "set_slot":
            mapped = _label_from_slot(cmd.name, cmd.value, transcript)
            if mapped:
                return mapped
    # C. conservative transcript cues
    return _label_from_transcript(transcript)


# --- Evidence / resolution -------------------------------------------------------
def update_evidence(
    label_state: LabelStateModel, current_label: str, turn_no: int
) -> LabelStateModel:
    label_state.evidence_by_label[current_label] = (
        label_state.evidence_by_label.get(current_label, 0) + 1
    )
    if current_label in HIGH_RISK_LABELS:
        existing = next(
            (u for u in label_state.unresolved_high_risk_labels if u.label == current_label),
            None,
        )
        if existing is None:
            label_state.unresolved_high_risk_labels.append(
                UnresolvedRisk(label=current_label, since_turn=turn_no, evidence=1)
            )
        elif existing.resolution is None:
            existing.evidence += 1
    # NB: high-risk labels are never decayed during the same call (by design).
    return label_state


def is_high_risk_unresolved(
    label_state: LabelStateModel, current_label: str, high_risk_block: bool
) -> bool:
    if not high_risk_block:
        return False
    if current_label not in MONEY_PATH_LABELS:
        return False
    return any(u.resolution is None for u in label_state.unresolved_high_risk_labels)


def resolve_previous_label(
    label_state: LabelStateModel, label: str | None, reason: str
) -> LabelStateModel:
    if not label:
        return label_state
    for u in label_state.unresolved_high_risk_labels:
        if u.label == label and u.resolution is None:
            u.resolution = reason
    if label not in label_state.resolved_labels:
        label_state.resolved_labels.append(label)
    return label_state


# --- Decision --------------------------------------------------------------------
def transition_allowed(
    provider: LabelTransitionProviderInfo,
    previous_label: str | None,
    current_label: str,
    state: ConversationState,
    label_state: LabelStateModel,
    transcript: str,
    high_risk_block: bool,
) -> str:
    if current_label == previous_label:
        return Decision.CONTINUE_CURRENT_FLOW

    # Opt-out defers to the compliance gate; we only record it.
    if current_label == Label.COMPLIANCE_OPT_OUT:
        return Decision.BLOCK_SWITCH_DUE_TO_HIGH_RISK

    money = current_label in MONEY_PATH_LABELS
    if money and high_risk_block:
        unresolved = {
            u.label for u in label_state.unresolved_high_risk_labels if u.resolution is None
        }
        if unresolved:
            disputes = {lbl for lbl in unresolved if label_namespace(lbl) == "dispute"}
            identity = {lbl for lbl in unresolved if label_namespace(lbl) == "identity"}
            allow_pay = {Label.RISK_LEGAL_THREAT, Label.RISK_HARASSMENT_COMPLAINT}
            if disputes:
                if Label.DISPUTE_FRAUD in disputes:
                    return Decision.ESCALATE_TO_HUMAN
                if has_ownership_confirmation(transcript) and (
                    Label.DISPUTE_LOAN_NOT_TAKEN in disputes
                ):
                    return Decision.RESOLVE_PREVIOUS_AND_SWITCH
                return Decision.CLARIFY_BEFORE_SWITCH
            if identity:
                if Label.IDENTITY_WRONG_PERSON in identity:
                    return Decision.BLOCK_SWITCH_DUE_TO_HIGH_RISK
                return Decision.CLARIFY_BEFORE_SWITCH  # third_party
            if unresolved & allow_pay:
                return Decision.KEEP_HIGH_RISK_FLAG_BUT_ALLOW_PAYMENT
            return Decision.BLOCK_SWITCH_DUE_TO_HIGH_RISK

    if previous_label is None:
        return (
            Decision.SWITCH_FLOW
            if provider.label_to_flow.get(current_label)
            else Decision.CONTINUE_CURRENT_FLOW
        )

    pns = label_namespace(previous_label)
    if pns in ("refusal", "hardship") and money:
        return Decision.RESOLVE_PREVIOUS_AND_SWITCH

    if provider.label_to_flow.get(current_label):
        return Decision.SWITCH_FLOW
    return Decision.CONTINUE_CURRENT_FLOW


# --- Command building (enforce only) ---------------------------------------------
def _is_money_path_start_flow(
    cmd: Command, provider: LabelTransitionProviderInfo
) -> bool:
    if cmd.command != "start_flow" or not cmd.flow:
        return False
    lbl = provider.flow_to_label.get(cmd.flow)
    return bool(lbl and lbl in MONEY_PATH_LABELS)


def _drop_start_flows(commands: list[Command]) -> list[Command]:
    return [c for c in commands if c.command != "start_flow"]


def _has_start_flow(commands: list[Command], flow: str) -> bool:
    return any(c.command == "start_flow" and c.flow == flow for c in commands)


def build_transition_commands(
    decision: str,
    commands: list[Command],
    current_label: str,
    label_state: LabelStateModel,
    provider: LabelTransitionProviderInfo,
    flows: FlowSet,
) -> tuple[list[Command], str | None]:
    """Return (commands, skipped_reason). Only called in enforce mode. Uses only real
    flows present in the FlowSet; never invents flow names."""
    target = provider.label_to_flow.get(current_label)
    target_exists = bool(target and target in flows.flows)

    if decision in (Decision.SWITCH_FLOW, Decision.RESOLVE_PREVIOUS_AND_SWITCH,
                    Decision.ACCUMULATE_EVIDENCE):
        if not target:
            return commands, "no_target_flow_for_label"
        if not target_exists:
            return commands, "target_flow_missing_from_flowset"
        if _has_start_flow(commands, target):
            return commands, None  # already routed; no change needed
        new_cmds = _drop_start_flows(commands) + [Command(command="start_flow", flow=target)]
        return new_cmds, None

    if decision == Decision.CLARIFY_BEFORE_SWITCH:
        new_cmds = [c for c in commands if not _is_money_path_start_flow(c, provider)]
        new_cmds.append(Command(command="clarify"))
        return new_cmds, None

    if decision == Decision.BLOCK_SWITCH_DUE_TO_HIGH_RISK:
        # Remove any payment/link start_flow; route to the real risk flow if one exists,
        # otherwise clarify (never invent a flow).
        new_cmds = [c for c in commands if not _is_money_path_start_flow(c, provider)]
        risk_flow = _real_risk_flow(label_state, provider, flows)
        if risk_flow and not _has_start_flow(new_cmds, risk_flow):
            new_cmds = _drop_start_flows(new_cmds) + [
                Command(command="start_flow", flow=risk_flow)
            ]
            return new_cmds, None
        new_cmds.append(Command(command="clarify"))
        return new_cmds, None

    if decision == Decision.ESCALATE_TO_HUMAN:
        # No standalone SOT transfer flow exists; route to a real risk flow that itself
        # transfers if available, else clarify + defer to existing guards.
        risk_flow = _real_risk_flow(label_state, provider, flows)
        if risk_flow:
            new_cmds = _drop_start_flows(commands) + [
                Command(command="start_flow", flow=risk_flow)
            ]
            return new_cmds, None
        new_cmds = _drop_start_flows(commands) + [Command(command="clarify")]
        return new_cmds, "no_transfer_flow_defer_to_guards"

    # continue_current_flow / keep_high_risk_flag_but_allow_payment / noop -> unchanged
    return commands, None


def _real_risk_flow(
    label_state: LabelStateModel, provider: LabelTransitionProviderInfo, flows: FlowSet
) -> str | None:
    """Map the worst unresolved high-risk label to a real flow, if one exists."""
    for u in label_state.unresolved_high_risk_labels:
        if u.resolution is not None:
            continue
        flow = provider.label_to_flow.get(u.label)
        if flow and flow in flows.flows:
            return flow
    return None


# --- Logging ---------------------------------------------------------------------
def record_blocked_transition(
    label_state: LabelStateModel,
    turn_no: int,
    requested_action: str | None,
    blocked_by: str | None,
    decision: str,
    reason: str | None,
    provider_name: str,
    enforcement_applied: bool,
) -> LabelStateModel:
    label_state.blocked_transitions.append(
        BlockedTransition(
            turn=turn_no,
            requested_action=requested_action,
            blocked_by=blocked_by,
            decision=decision,
            reason=reason,
            provider=provider_name,
            enforcement_applied=enforcement_applied,
        )
    )
    return label_state


def write_label_event(
    state: ConversationState,
    label_state: LabelStateModel,
    decision: TransitionDecision,
    turn_no: int,
    log_enabled: bool,
) -> ConversationState:
    item = LabelHistoryItem(
        turn=turn_no,
        previous_label=decision.previous_label,
        current_label=decision.current_label,
        decision=decision.decision,
        reason=decision.reason,
        provider=decision.provider,
        mode=decision.mode,
        target_flow=decision.target_flow,
        blocked_by=decision.blocked_by,
        enforcement_applied=decision.enforcement_applied,
    )
    label_state.label_history.append(item)
    if len(label_state.label_history) > _MAX_HISTORY:
        label_state.label_history = label_state.label_history[-_MAX_HISTORY:]
    if log_enabled:
        state.events.append(
            Event(
                ts=datetime.now(timezone.utc).isoformat(),
                kind="label_transition",
                data=decision.model_dump(mode="json"),
            )
        )
    return state


def apply_transition_decision(
    label_state: LabelStateModel,
    decision_str: str,
    previous_label: str | None,
    current_label: str,
    reason: str,
) -> LabelStateModel:
    if decision_str == Decision.RESOLVE_PREVIOUS_AND_SWITCH and previous_label:
        label_state = resolve_previous_label(label_state, previous_label, reason)
    # keep_high_risk_flag_but_allow_payment: explicitly do NOT resolve.
    label_state.previous_label = label_state.active_label
    label_state.active_label = current_label
    return label_state


# --- Orchestrator ----------------------------------------------------------------
def run_label_transition(
    *,
    state: ConversationState,
    commands: list[Command],
    transcript: str,
    awaiting_slot: str,
    candidate_flows: object,
    tenant_id: str,
    flows: FlowSet,
    settings: object,
    dispute_theme: str | None = None,
    dispute_forced: str | None = None,
) -> tuple[ConversationState, list[Command], TransitionDecision | None]:
    """Provider-aware LTL entry point. See module docstring for behavior."""
    if not getattr(settings, "label_transition_enabled", False):
        return state, commands, None

    scope = getattr(settings, "label_transition_scope", "supported")
    requested_mode = getattr(settings, "label_transition_mode", "shadow")
    high_risk_block = bool(getattr(settings, "label_high_risk_block", True))
    log_enabled = bool(getattr(settings, "label_transition_log_enabled", True))

    provider = get_label_transition_provider(tenant_id, settings)

    # scope=supported: only run for providers that support enforce (i.e. SOT). scope=all:
    # run shadow everywhere; enforce still restricted to enforce-capable providers.
    if scope == "supported" and not provider.supports_enforce:
        return state, commands, None

    label_state = load_label_state(state)
    label_state.provider = provider.name

    current_label = detect_current_labels(
        commands, awaiting_slot, transcript, candidate_flows, provider
    )
    if current_label is None:
        # Nothing detected — do not churn state.
        return state, commands, None

    turn_no = state.attempts
    previous_label = label_state.active_label

    label_state = update_evidence(label_state, current_label, turn_no)

    decision_str = transition_allowed(
        provider,
        previous_label,
        current_label,
        state,
        label_state,
        transcript,
        high_risk_block,
    )

    reason = _reason_for(decision_str, previous_label, current_label)
    target_flow = provider.label_to_flow.get(current_label)
    blocked_by = _blocked_by(label_state, decision_str)

    # Effective mode: enforce only if requested AND provider supports it.
    enforce = requested_mode == "enforce" and provider.supports_enforce
    enforcement_applied = False
    skipped_reason: str | None = None
    if requested_mode == "enforce" and not provider.supports_enforce:
        skipped_reason = "unsupported_provider"
    elif not enforce:
        skipped_reason = "shadow_mode"

    if enforce:
        new_commands, build_skip = build_transition_commands(
            decision_str, commands, current_label, label_state, provider, flows
        )
        if build_skip:
            skipped_reason = build_skip
        if new_commands != commands:
            commands = new_commands
            enforcement_applied = True

    label_state.mode = "enforce" if enforce else "shadow"
    label_state.enforce_applied = enforcement_applied

    # Evolve label state identically in shadow + enforce (so shadow is a faithful preview).
    label_state = apply_transition_decision(
        label_state, decision_str, previous_label, current_label, reason
    )

    if decision_str in (
        Decision.CLARIFY_BEFORE_SWITCH,
        Decision.BLOCK_SWITCH_DUE_TO_HIGH_RISK,
        Decision.ESCALATE_TO_HUMAN,
    ):
        label_state = record_blocked_transition(
            label_state,
            turn_no,
            requested_action=_requested_action(current_label),
            blocked_by=blocked_by,
            decision=decision_str,
            reason=reason,
            provider_name=provider.name,
            enforcement_applied=enforcement_applied,
        )

    decision = TransitionDecision(
        decision=decision_str,
        provider=provider.name,
        mode=label_state.mode,
        current_label=current_label,
        previous_label=previous_label,
        reason=reason,
        target_flow=target_flow,
        blocked_by=blocked_by,
        enforcement_applied=enforcement_applied,
        enforcement_skipped_reason=skipped_reason,
    )

    state = write_label_event(state, label_state, decision, turn_no, log_enabled)
    state = save_label_state(state, label_state)
    return state, commands, decision


def _requested_action(current_label: str) -> str:
    if current_label in (Label.SUPPORT_PAYMENT_LINK_REQUEST, Label.SUPPORT_DIFF_NUMBER_LINK):
        return "send_payment_link"
    if label_namespace(current_label) == "payment":
        return "take_payment"
    return "switch_flow"


def _blocked_by(label_state: LabelStateModel, decision_str: str) -> str | None:
    if decision_str not in (
        Decision.CLARIFY_BEFORE_SWITCH,
        Decision.BLOCK_SWITCH_DUE_TO_HIGH_RISK,
        Decision.ESCALATE_TO_HUMAN,
    ):
        return None
    for u in label_state.unresolved_high_risk_labels:
        if u.resolution is None:
            return u.label
    return None


def _reason_for(decision_str: str, previous_label: str | None, current_label: str) -> str:
    return f"{decision_str}: {previous_label or 'none'} -> {current_label}"
