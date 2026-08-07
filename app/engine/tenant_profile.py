"""Declarative TenantRuntimeProfile — scripted-tenant behaviour as data.

Loaded from ``app/tenants/<tenant_id>.yml``. ``get_tenant_profile(tenant_id)``
returns ``None`` for open/default tenants (no scripted coercions / on-rails).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

TENANTS_DIR = Path(__file__).resolve().parent.parent / "tenants"


class TenantRuntimeProfile(BaseModel):
    tenant_id: str
    flow_prefix: str
    # Explicit objection flow-name prefix (e.g. "sot_obj_"). Defaults to
    # ``f"{flow_prefix}obj_"`` when omitted from YAML.
    objection_prefix: str = ""
    onrails_flows: frozenset[str] = Field(default_factory=frozenset)
    commit_collect_slots: frozenset[str] = Field(default_factory=frozenset)
    push_intent_slots: frozenset[str] = Field(default_factory=frozenset)
    reversal_slots: frozenset[str] = Field(default_factory=frozenset)
    main_ladder_prefixes: tuple[str, ...] = ()
    blocked_commands: frozenset[str] = Field(default_factory=frozenset)
    deflection_objections: frozenset[str] = Field(default_factory=frozenset)
    pinned_flows: list[str] = Field(default_factory=list)
    dispute_flows: list[str] = Field(default_factory=list)
    coercion_chain: list[str] = Field(default_factory=list)
    cue_packs: dict[str, list[str]] = Field(default_factory=dict)
    respond_enabled: bool = False
    # Verbatim fallback when respond text fails fact-grounding or facts are missing.
    unknown_info_reply: str = ""
    # D-1 option (c): pressure phrases approved for this tenant's script copy.
    # Exact substring (word-bounded) on normalized text; empty = no exemptions.
    gate_allowlisted_phrases: list[str] = Field(default_factory=list)
    scenario_selector: str = "due_date"
    frustration_escalate_turns: int = 3
    # Slot / flow names that vary per tenant but are fixed for a given script.
    reversal_target_flow: str = ""
    identity_slot: str = ""
    final_confirm_slot: str = ""
    link_received_slot: str = ""
    call_closed_slot: str = ""
    # Free-text reason / hardship catchall (SOT: sot_payment_problem; PaisaLo: plo_timeline).
    reason_slot: str = ""
    dispute_loan_tokens: list[str] = Field(default_factory=list)
    dispute_theme_flows: dict[str, str] = Field(default_factory=dict)

    @field_validator(
        "onrails_flows",
        "commit_collect_slots",
        "push_intent_slots",
        "reversal_slots",
        "blocked_commands",
        "deflection_objections",
        mode="before",
    )
    @classmethod
    def _as_frozenset(cls, value: Any) -> frozenset[str]:
        if value is None:
            return frozenset()
        if isinstance(value, frozenset):
            return value
        return frozenset(str(v) for v in value)

    @field_validator("main_ladder_prefixes", mode="before")
    @classmethod
    def _as_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        return tuple(str(v) for v in value)

    def cues(self, name: str) -> tuple[str, ...]:
        return tuple(self.cue_packs.get(name) or ())

    def cue_set(self, name: str) -> frozenset[str]:
        return frozenset(self.cue_packs.get(name) or ())


def _profile_path(tenant_id: str) -> Path:
    return TENANTS_DIR / f"{tenant_id}.yml"


def _normalize_cue_packs(raw: dict[str, Any]) -> None:
    """Expand ``intent_refusal`` as refusal ∪ intent_refusal_extras (no YAML dupe)."""
    packs = raw.get("cue_packs")
    if not isinstance(packs, dict):
        return
    extras = packs.get("intent_refusal_extras")
    if extras is None:
        return
    refusal = list(packs.get("refusal") or [])
    # Preserve order: full refusal list, then extras not already present.
    seen = set(refusal)
    merged = list(refusal)
    for cue in extras:
        if cue not in seen:
            merged.append(cue)
            seen.add(cue)
    packs["intent_refusal"] = merged


def load_tenant_profile(tenant_id: str, *, path: Path | None = None) -> TenantRuntimeProfile | None:
    """Load and validate a tenant YAML. Returns None if the file is absent."""
    profile_path = path or _profile_path(tenant_id)
    if not profile_path.is_file():
        return None
    raw = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid tenant profile: {profile_path}")
    raw.setdefault("tenant_id", tenant_id)
    if not raw.get("objection_prefix"):
        raw["objection_prefix"] = f"{raw.get('flow_prefix', '')}obj_"
    _normalize_cue_packs(raw)
    return TenantRuntimeProfile.model_validate(raw)


@lru_cache(maxsize=32)
def get_tenant_profile(tenant_id: str) -> TenantRuntimeProfile | None:
    """Cached registry lookup. ``None`` → open/default tenant (no scripted profile)."""
    if not tenant_id:
        return None
    return load_tenant_profile(tenant_id)


def clear_tenant_profile_cache() -> None:
    """Test helper — drop the LRU so reloads pick up YAML edits."""
    get_tenant_profile.cache_clear()
