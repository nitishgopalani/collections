"""Optional per-agent flow routing — default tenant/routing unchanged for other agents."""

from __future__ import annotations

# agent_id -> (forced_flow_id, tenant_id)
# The "test-*" agents pin a specific scenario flow for deterministic live QA. Identity
# verification still runs first (the forced collection flow is parked until identity_ok),
# then the chosen scenario runs end-to-end.
AGENT_FORCE_FLOW_MAP: dict[str, tuple[str, str]] = {
    "identity-name-confirm": ("identity_name_confirm", "test-name-identity"),
    "test-simple-ptp": ("simple_ptp_test", "test-simple-ptp"),
    "test-pay-now": ("pay_now", "default"),
    "test-ptp": ("promise_to_pay", "default"),
    "test-partial": ("partial_payment", "default"),
    "test-dispute": ("dispute", "default"),
    "test-hardship": ("hardship", "default"),
    "salary-on-time": ("sot_opener", "salary_on_time"),
    "salary-on-time-test": ("sot_opener", "salary_on_time"),
    "paisalo": ("plo_opener", "paisalo"),
    "paisalo-test": ("plo_opener", "paisalo"),
}

FORCE_FLOW_ALIASES: frozenset[str] = frozenset(
    {
        "identity_name_confirm",
        "simple_ptp_test",
        "pay_now",
        "promise_to_pay",
        "partial_payment",
        "dispute",
        "hardship",
        "sot_opener",
        "plo_opener",
        "sot_offer_pre_closure",
        "sot_push",
        "sot_commit",
        "sot_close",
        "sot_obj_never_loan",
        "sot_obj_medical",
        "sot_obj_job_loss",
        "sot_obj_no_timeline",
        "sot_obj_wrong_amount",
        "sot_obj_frozen_account",
        "sot_obj_death",
        "sot_obj_out_of_station",
        "sot_obj_credit_manager",
        "sot_obj_references_called",
        "sot_obj_pending_status",
        "sot_obj_cant_login",
        "sot_obj_enhanced_amount",
        "sot_obj_penalty",
        "sot_obj_processing_fee",
        "sot_obj_high_interest",
        "sot_obj_link_request",
        "sot_obj_month_only",
        "sot_obj_support_number",
        "sot_obj_busy",
        "sot_obj_hold",
        "sot_obj_wont_pay",
        "sot_obj_cash",
        "sot_obj_reduce_amount",
        "sot_obj_no_link_pref",
        "sot_obj_where_from",
        "sot_obj_pay_later_today",
        "sot_obj_is_bot",
        "sot_obj_diff_number_link",
        "sot_obj_already_paid_q",
        "sot_obj_unknown_query",
    }
)


def resolve_agent_routing(agent_id: str) -> tuple[str | None, str | None]:
    """Return (force_flow, tenant_id) when agent_id selects an isolated test/prod-style path."""
    entry = AGENT_FORCE_FLOW_MAP.get(agent_id.strip())
    if entry is None:
        return None, None
    return entry[0], entry[1]


def resolve_session_tenant(
    *,
    client_id: str,
    routed_tenant: str | None,
    inbound_tenant_id: str | None,
    default_tenant_id: str,
) -> tuple[str, str]:
    """Resolve the owning tenant for a session, and report which signal decided it.

    Priority (Phase C):
      1. ``client_id`` from the connector, when non-empty — the explicit tenant id.
      2. ``routed_tenant`` from agent-id routing (pre-Phase-C behaviour).
      3. ``inbound_tenant_id`` explicit ``tenant_id`` field on session_start.
      4. ``default_tenant_id`` (preserves single-tenant fallback).

    When ``client_id`` is absent, this collapses to the exact pre-Phase-C chain
    (routed -> tenant_id -> default), so existing callers are unaffected.

    Returns ``(tenant_id, source)`` where ``source`` is one of
    ``client_id | agent_routing | session_tenant_id | default``.
    """
    cid = (client_id or "").strip()
    if cid:
        return cid, "client_id"
    if routed_tenant:
        return routed_tenant, "agent_routing"
    if inbound_tenant_id:
        return inbound_tenant_id, "session_tenant_id"
    return default_tenant_id, "default"


def resolve_session_defaults(
    *,
    default_pack_id: str,
    default_agent_id: str,
    default_locale: str,
    explicit_pack_id: str,
    explicit_agent_id: str,
    explicit_locale: str,
) -> tuple[str, str, str]:
    """Fill pack_id / agent_id / locale from tenant defaults when omitted.

    Explicit (caller-provided) values always win; tenant defaults only fill gaps.
    ``locale`` falls back to ``hi-IN`` when neither is provided (unchanged default).
    Returns ``(pack_id, agent_id, locale)``.
    """
    pack_id = (explicit_pack_id or "").strip() or (default_pack_id or "")
    agent_id = (explicit_agent_id or "").strip() or (default_agent_id or "")
    locale = (explicit_locale or "").strip() or (default_locale or "") or "hi-IN"
    return pack_id, agent_id, locale
