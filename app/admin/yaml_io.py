"""Read/write tenant YAML (the Brand Console database)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from app.engine.fragment_library import clear_fragment_cache
from app.engine.tenant_profile import TENANTS_DIR, clear_tenant_profile_cache

EDITABLE_PROFILE_KEYS = (
    "dpdp_third_party_lock",
    "dpdp_disclosure_tier_enforced",
    "call_window_start",
    "call_window_end",
    "call_window_timezone",
    "ptp_policy",
    "frustration_escalate_turns",
    "max_slot_retries",
    "scenario_voices",
    "scenario_pace",
    "variant_tone",
)

_HHMM = re.compile(r"^\d{2}:\d{2}$")
_SCENARIOS = ("predue", "ondue", "postdue1", "postdue2", "postdue3", "npa")
DEFAULT_VOICES = {
    "predue": "simran",
    "ondue": "simran",
    "postdue1": "neha",
    "postdue2": "neha",
    "postdue3": "kabir",
    "npa": "amit",
}
DEFAULT_PACE = {
    "predue": 1.1,
    "ondue": 1.1,
    "postdue1": 1.1,
    "postdue2": 1.1,
    "postdue3": 0.95,
    "npa": 1.0,
}
VOICE_CATALOG = ("simran", "neha", "kabir", "amit", "anushka", "priya")


def profile_path(tenant_id: str) -> Path:
    return TENANTS_DIR / f"{tenant_id}.yml"


def fragments_path(tenant_id: str) -> Path:
    return TENANTS_DIR / f"{tenant_id}_fragments.yml"


def list_tenant_ids() -> list[str]:
    ids: list[str] = []
    for path in sorted(TENANTS_DIR.glob("*.yml")):
        name = path.name
        if name.endswith("_fragments.yml") or name.endswith("_irrelevant_topics.yml"):
            continue
        ids.append(path.stem)
    return ids


def load_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(str(path))
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("YAML root must be a mapping")
    return data


def dump_raw(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    clear_tenant_profile_cache()
    clear_fragment_cache()


def validate_profile_patch(patch: dict[str, Any]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    lock = patch.get("dpdp_third_party_lock")
    if lock is not None and str(lock) not in {"strict", "relaxed"}:
        errors.append({"field": "dpdp_third_party_lock", "error": "must be strict or relaxed"})
    for key in ("call_window_start", "call_window_end"):
        val = patch.get(key)
        if val and not _HHMM.match(str(val)):
            errors.append({"field": key, "error": "must be HH:MM"})
    ptp = patch.get("ptp_policy")
    if isinstance(ptp, dict):
        days = ptp.get("max_ptp_days")
        if days is not None and not (1 <= int(days) <= 90):
            errors.append({"field": "ptp_policy.max_ptp_days", "error": "must be 1-90"})
        pct = ptp.get("min_partial_pct")
        if pct is not None and not (0 <= int(pct) <= 100):
            errors.append({"field": "ptp_policy.min_partial_pct", "error": "must be 0-100"})
        att = ptp.get("counter_max_attempts")
        if att is not None and not (0 <= int(att) <= 5):
            errors.append({"field": "ptp_policy.counter_max_attempts", "error": "must be 0-5"})
    retries = patch.get("max_slot_retries")
    if retries is not None and not (0 <= int(retries) <= 10):
        errors.append({"field": "max_slot_retries", "error": "must be 0-10"})
    turns = patch.get("frustration_escalate_turns")
    if turns is not None and not (0 <= int(turns) <= 20):
        errors.append({"field": "frustration_escalate_turns", "error": "must be 0-20"})
    voices = patch.get("scenario_voices")
    if isinstance(voices, dict):
        for scen, voice in voices.items():
            if scen not in _SCENARIOS:
                errors.append({"field": f"scenario_voices.{scen}", "error": "unknown scenario"})
            if voice and str(voice) not in VOICE_CATALOG:
                errors.append({"field": f"scenario_voices.{scen}", "error": "unknown voice"})
    pace = patch.get("scenario_pace")
    if isinstance(pace, dict):
        for scen, val in pace.items():
            try:
                num = float(val)
            except (TypeError, ValueError):
                errors.append({"field": f"scenario_pace.{scen}", "error": "must be a number"})
                continue
            if not (0.5 <= num <= 2.0):
                errors.append({"field": f"scenario_pace.{scen}", "error": "must be 0.5-2.0"})
    return errors


def apply_profile_patch(raw: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(raw)
    for key in EDITABLE_PROFILE_KEYS:
        if key not in patch:
            continue
        val = patch[key]
        if key == "ptp_policy" and isinstance(val, dict):
            merged = dict(out.get("ptp_policy") or {})
            merged.update(val)
            out["ptp_policy"] = merged
        elif key in {"scenario_voices", "scenario_pace"} and isinstance(val, dict):
            merged = dict(out.get(key) or {})
            merged.update(val)
            out[key] = merged
        else:
            out[key] = val
    return out
