"""Tier-2 tenant flow catalog — full scripted-tenant menu for command routing.

Replaces KB retrieval + pinning for profiled tenants. Cache key is
``(tenant_id, flow_set_version)``; ``reload_flow_set`` bumps the version and
clears the cache.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.engine.tenant_profile import TenantRuntimeProfile
from app.schemas.flow import FlowSet
from app.schemas.state import ConversationState

logger = logging.getLogger(__name__)

# Bumped by ``bump_flow_set_version`` (wired from ``reload_flow_set``).
_FLOW_SET_VERSION: int = 0
_CATALOG_CACHE: dict[tuple[str, int], list[dict[str, str]]] = {}
_TOKEN_LOGGED: set[str] = set()

_TRIGGER_SPLIT = re.compile(r"(?i)\btriggers?\s*:\s*")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def get_flow_set_version() -> int:
    return _FLOW_SET_VERSION


def bump_flow_set_version() -> int:
    """Invalidate catalog cache after a flow-set reload. Returns new version."""
    global _FLOW_SET_VERSION
    _FLOW_SET_VERSION += 1
    _CATALOG_CACHE.clear()
    return _FLOW_SET_VERSION


def clear_catalog_cache() -> None:
    """Test helper — drop cached catalogs without bumping the version."""
    _CATALOG_CACHE.clear()
    _TOKEN_LOGGED.clear()


def _truncate_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text.strip()
    return " ".join(words[:max_words]).strip()


def compress_flow_description(description: str, *, max_words: int = 25) -> str:
    """First sentence + trigger phrases, each truncated to ``max_words`` words."""
    raw = (description or "").strip()
    if not raw:
        return ""
    parts = _TRIGGER_SPLIT.split(raw, maxsplit=1)
    head = parts[0].strip()
    triggers = parts[1].strip() if len(parts) > 1 else ""
    sentences = _SENTENCE_END.split(head, maxsplit=1)
    first = _truncate_words(sentences[0].strip(), max_words)
    if not triggers:
        return first
    trig = _truncate_words(triggers.rstrip("."), max_words)
    if not first:
        return f"Triggers: {trig}" if trig else ""
    return f"{first} Triggers: {trig}" if trig else first


def estimate_catalog_tokens(catalog: list[dict[str, str]]) -> int:
    """Rough token estimate: ``len(json)/4`` as specified in the tracker."""
    return max(1, len(json.dumps(catalog, ensure_ascii=False)) // 4)


def _flow_belongs_to_tenant(name: str, profile: TenantRuntimeProfile) -> bool:
    """Match ``flow_prefix`` and any ``main_ladder_prefixes`` (e.g. sotod_/sotpd_)."""
    if name.startswith(profile.flow_prefix):
        return True
    return any(name.startswith(p) for p in profile.main_ladder_prefixes)


def build_tenant_flow_catalog(
    profile: TenantRuntimeProfile, flows: FlowSet
) -> list[dict[str, str]]:
    """Tenant flows as ``{name, description}`` (no score).

    Includes ``flow_prefix`` matches plus ``main_ladder_prefixes`` (SOT on-due /
    post-due packs use ``sotod_`` / ``sotpd_``, which do not start with ``sot_``).
    """
    entries: list[dict[str, str]] = []
    for name in sorted(flows.flows):
        if not _flow_belongs_to_tenant(name, profile):
            continue
        flow = flows.flows[name]
        entries.append(
            {
                "name": name,
                "description": compress_flow_description(flow.description),
            }
        )
    return entries


def tenant_flow_catalog(
    profile: TenantRuntimeProfile, flows: FlowSet
) -> list[dict[str, str]]:
    """Cached catalog for a scripted tenant. Key = (tenant_id, flow_set_version)."""
    key = (profile.tenant_id, _FLOW_SET_VERSION)
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return list(cached)

    built = build_tenant_flow_catalog(profile, flows)
    _CATALOG_CACHE[key] = built
    if profile.tenant_id not in _TOKEN_LOGGED:
        tokens = estimate_catalog_tokens(built)
        logger.info(
            "tenant_flow_catalog ready tenant=%s flows=%d tokens≈%d version=%d",
            profile.tenant_id,
            len(built),
            tokens,
            _FLOW_SET_VERSION,
        )
        _TOKEN_LOGGED.add(profile.tenant_id)
    return list(built)


def normalize_scenario_key(raw: Any) -> str | None:
    """Collapse postdue1/2/3 -> postdue. Empty -> None."""
    if raw is None:
        return None
    key = str(raw).strip().lower()
    if not key:
        return None
    if key.startswith("postdue"):
        return "postdue"
    return key


def infer_scenario_key(state: ConversationState, flows: FlowSet) -> str | None:
    """Current scenario from the ``plo_scenario`` slot, else the active flow's YAML tag."""
    slotted = normalize_scenario_key(state.slots.get("plo_scenario"))
    if slotted:
        return slotted
    if not state.flow_stack:
        return None
    active = flows.flows.get(state.flow_stack[-1].flow)
    if active is None or not active.scenarios:
        return None
    return normalize_scenario_key(active.scenarios[0])


def _tenant_flow_is_tagged(flow: Any) -> bool:
    return bool(
        getattr(flow, "scenarios", None)
        or getattr(flow, "valid_slots", None)
        or getattr(flow, "catalog_scope", None)
    )


def build_scoped_catalog(
    profile: TenantRuntimeProfile,
    flows: FlowSet,
    state: ConversationState,
    awaiting_slot: str = "",
) -> list[dict[str, str]]:
    """D3: state-scoped catalog from flow YAML metadata.

    Offers: current scenario's flows + objections valid for the awaited
    slot + ``catalog_scope=universal``. If the tenant has no scenario/slot
    tags, return the full catalog (SOT and untagged tenants unchanged).
    If the current scenario is unknown, return the full catalog (safe).
    """
    full = tenant_flow_catalog(profile, flows)
    tagged = 0
    for entry in full:
        flow = flows.flows.get(str(entry.get("name") or ""))
        if flow is not None and _tenant_flow_is_tagged(flow):
            tagged += 1
    scenario = infer_scenario_key(state, flows)
    if tagged == 0 or scenario is None:
        return full

    active = state.flow_stack[-1].flow if state.flow_stack else ""
    scoped: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in full:
        name = str(entry.get("name") or "")
        flow = flows.flows.get(name)
        if flow is None or name in seen:
            continue
        include = False
        if name == active:
            include = True
        elif (flow.catalog_scope or "") == "universal":
            include = True
        else:
            scenarios = [normalize_scenario_key(s) for s in (flow.scenarios or [])]
            slot_ok = (
                not flow.valid_slots
                or not awaiting_slot
                or awaiting_slot in flow.valid_slots
            )
            if scenario in scenarios and slot_ok:
                include = True
            elif flow.valid_slots and awaiting_slot in flow.valid_slots:
                include = True
        if include:
            scoped.append(entry)
            seen.add(name)
    return scoped or full


def filter_deflection_objections(
    catalog: list[dict[str, Any]],
    profile: TenantRuntimeProfile,
) -> list[dict[str, Any]]:
    """Drop ``profile.deflection_objections``; disputes + info objections stay."""
    blocked = profile.deflection_objections
    if not blocked:
        return list(catalog)
    return [c for c in catalog if str(c.get("name", "")) not in blocked]
