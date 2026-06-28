"""Optional per-agent flow routing — default tenant/routing unchanged for other agents."""

from __future__ import annotations

# agent_id -> (forced_flow_id, tenant_id)
AGENT_FORCE_FLOW_MAP: dict[str, tuple[str, str]] = {
    "identity-name-confirm": ("identity_name_confirm", "test-name-identity"),
    "test-simple-ptp": ("simple_ptp_test", "test-simple-ptp"),
}

FORCE_FLOW_ALIASES: frozenset[str] = frozenset(
    {
        "identity_name_confirm",
        "simple_ptp_test",
    }
)


def resolve_agent_routing(agent_id: str) -> tuple[str | None, str | None]:
    """Return (force_flow, tenant_id) when agent_id selects an isolated test/prod-style path."""
    entry = AGENT_FORCE_FLOW_MAP.get(agent_id.strip())
    if entry is None:
        return None, None
    return entry[0], entry[1]
