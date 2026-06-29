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
    }
)


def resolve_agent_routing(agent_id: str) -> tuple[str | None, str | None]:
    """Return (force_flow, tenant_id) when agent_id selects an isolated test/prod-style path."""
    entry = AGENT_FORCE_FLOW_MAP.get(agent_id.strip())
    if entry is None:
        return None, None
    return entry[0], entry[1]
