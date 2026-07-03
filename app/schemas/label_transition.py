"""Data-only schemas and constants for the Label Transition Layer (LTL).

Phase 1 is deterministic/rules-only. This module holds no logic and imports nothing
from ``app.engine`` (kept dependency-free to avoid import cycles). All routing logic
lives in ``app.engine.label_transition``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Decision:
    """Transition decisions the LTL can emit."""

    NOOP = "noop"
    SHADOW_ONLY = "shadow_only"
    CONTINUE_CURRENT_FLOW = "continue_current_flow"
    SWITCH_FLOW = "switch_flow"
    RESOLVE_PREVIOUS_AND_SWITCH = "resolve_previous_and_switch"
    CLARIFY_BEFORE_SWITCH = "clarify_before_switch"
    BLOCK_SWITCH_DUE_TO_HIGH_RISK = "block_switch_due_to_high_risk"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    KEEP_HIGH_RISK_FLAG_BUT_ALLOW_PAYMENT = "keep_high_risk_flag_but_allow_payment"
    ACCUMULATE_EVIDENCE = "accumulate_evidence"


class Label:
    """Dotted intent/risk labels (namespace.label)."""

    IDENTITY_WRONG_PERSON = "identity.wrong_person"
    IDENTITY_THIRD_PARTY = "identity.third_party"
    IDENTITY_CONFIRMED = "identity.confirmed"

    DISPUTE_LOAN_NOT_TAKEN = "dispute.loan_not_taken"
    DISPUTE_WRONG_AMOUNT = "dispute.wrong_amount"
    DISPUTE_ALREADY_PAID = "dispute.already_paid"
    DISPUTE_FRAUD = "dispute.fraud"

    REFUSAL_HARD = "refusal.hard_refusal"
    REFUSAL_SOFT = "refusal.soft_refusal"

    HARDSHIP_SALARY_NOT_RECEIVED = "hardship.salary_not_received"
    HARDSHIP_JOB_LOSS = "hardship.job_loss"
    HARDSHIP_MEDICAL = "hardship.medical"

    PAYMENT_WILL_PAY_TODAY = "payment.will_pay_today"
    PAYMENT_PROMISE_FUTURE_DATE = "payment.promise_future_date"
    PAYMENT_PARTIAL = "payment.partial"

    SUPPORT_PAYMENT_LINK_REQUEST = "support.payment_link_request"
    SUPPORT_DIFF_NUMBER_LINK = "support.diff_number_link"
    SUPPORT_NO_LINK_PREF = "support.no_link_pref"
    SUPPORT_CALLBACK_REQUEST = "support.callback_request"

    RISK_LEGAL_THREAT = "risk.legal_threat"
    RISK_HARASSMENT_COMPLAINT = "risk.harassment_complaint"
    RISK_VULNERABLE = "risk.vulnerable"
    RISK_SELF_HARM_SIGNAL = "risk.self_harm_signal"

    COMPLIANCE_OPT_OUT = "compliance.opt_out"

    EMOTION_FRUSTRATION = "emotion.frustration"


# High-risk labels never decay during the same call (persist until explicitly
# resolved, transferred, or the call ends).
HIGH_RISK_LABELS: frozenset[str] = frozenset(
    {
        Label.DISPUTE_LOAN_NOT_TAKEN,
        Label.DISPUTE_FRAUD,
        Label.DISPUTE_WRONG_AMOUNT,
        Label.DISPUTE_ALREADY_PAID,
        Label.IDENTITY_WRONG_PERSON,
        Label.IDENTITY_THIRD_PARTY,
        Label.RISK_LEGAL_THREAT,
        Label.RISK_HARASSMENT_COMPLAINT,
        Label.RISK_SELF_HARM_SIGNAL,
        Label.COMPLIANCE_OPT_OUT,
    }
)

# Labels that request money movement (payment or a payment link). A high-risk
# unresolved label blocks/clarifies a transition into one of these.
MONEY_PATH_LABELS: frozenset[str] = frozenset(
    {
        Label.PAYMENT_WILL_PAY_TODAY,
        Label.PAYMENT_PROMISE_FUTURE_DATE,
        Label.PAYMENT_PARTIAL,
        Label.SUPPORT_PAYMENT_LINK_REQUEST,
        Label.SUPPORT_DIFF_NUMBER_LINK,
    }
)


def label_namespace(label: str | None) -> str:
    """Return the namespace part of a dotted label ("dispute.x" -> "dispute")."""
    if not label:
        return ""
    return label.split(".", 1)[0]


class UnresolvedRisk(BaseModel):
    label: str
    since_turn: int = 0
    evidence: int = 1
    resolution: str | None = None  # None = still unresolved


class BlockedTransition(BaseModel):
    turn: int = 0
    requested_action: str | None = None
    blocked_by: str | None = None
    decision: str | None = None
    reason: str | None = None
    provider: str | None = None
    enforcement_applied: bool = False


class LabelHistoryItem(BaseModel):
    turn: int = 0
    previous_label: str | None = None
    current_label: str | None = None
    decision: str | None = None
    reason: str | None = None
    provider: str | None = None
    mode: str | None = None
    target_flow: str | None = None
    blocked_by: str | None = None
    enforcement_applied: bool = False


class LabelStateModel(BaseModel):
    """Persisted LTL state — stored JSON-serialized under state.slots["_label"]."""

    active_label: str | None = None
    previous_label: str | None = None
    label_history: list[LabelHistoryItem] = Field(default_factory=list)
    unresolved_high_risk_labels: list[UnresolvedRisk] = Field(default_factory=list)
    evidence_by_label: dict[str, int] = Field(default_factory=dict)
    resolved_labels: list[str] = Field(default_factory=list)
    blocked_transitions: list[BlockedTransition] = Field(default_factory=list)
    provider: str | None = None
    mode: str | None = None
    enforce_applied: bool = False


class TransitionDecision(BaseModel):
    decision: str
    provider: str | None = None
    mode: str | None = None
    current_label: str | None = None
    previous_label: str | None = None
    reason: str | None = None
    target_flow: str | None = None
    blocked_by: str | None = None
    enforcement_applied: bool = False
    enforcement_skipped_reason: str | None = None


class LabelTransitionProviderInfo(BaseModel):
    name: str
    supports_shadow: bool = True
    supports_enforce: bool = False
    label_to_flow: dict[str, str] = Field(default_factory=dict)
    flow_to_label: dict[str, str] = Field(default_factory=dict)
    unsupported_reason: str | None = None

    def as_summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "supports_shadow": self.supports_shadow,
            "supports_enforce": self.supports_enforce,
        }
