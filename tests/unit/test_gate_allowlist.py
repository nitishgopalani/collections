"""P4.5 — tenant-level collection-pressure allowlist (D-1 option c)."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.config import tenant_config
from app.engine.compliance_rules import evaluate_pressure_with_allowlist
from app.engine.gate import gate
from app.engine.tenant_profile import (
    TenantRuntimeProfile,
    clear_tenant_profile_cache,
    get_tenant_profile,
)
from app.engine.tracker import new_conversation_state


def _now(tenant_id: str = "default") -> datetime:
    cfg = tenant_config(tenant_id)
    return datetime(2026, 6, 25, 10, 0, tzinfo=ZoneInfo(cfg.call_window_timezone))


@pytest.fixture(autouse=True)
def _clear_profiles():
    clear_tenant_profile_cache()
    yield
    clear_tenant_profile_cache()


def test_salary_on_time_allowlist_empty():
    profile = get_tenant_profile("salary_on_time")
    assert profile is not None
    assert profile.gate_allowlisted_phrases == []


def test_allowlisted_phrase_passes_and_logs(monkeypatch, caplog):
    """Full allowlisted sentence covering pressure spans → allow + gate_warnings."""
    allow = "please emi jama karna hoga"

    def _profile(tenant_id: str):
        return TenantRuntimeProfile(
            tenant_id=tenant_id,
            flow_prefix="x_",
            gate_allowlisted_phrases=[allow],
        )

    monkeypatch.setattr("app.engine.gate.get_tenant_profile", _profile)
    caplog.set_level(logging.INFO, logger="app.engine.gate")

    state = new_conversation_state("call-al", "default", "b1")
    state.slots["compliance_flags"] = {"dispute_hold": True}
    cfg = tenant_config("default")
    text = "Please EMI jama karna hoga"
    result = gate(text, state, cfg, now=_now())

    assert result.verdict == "allow"
    assert result.text == text
    assert result.warnings
    assert all(w.get("allowlisted") is True for w in result.warnings)
    assert any(w.get("phrase") == "jama karna" for w in result.warnings)

    warning_logs = [
        r for r in caplog.records if r.getMessage().startswith("gate_warnings ")
    ]
    assert warning_logs
    payload = json.loads(warning_logs[0].getMessage().removeprefix("gate_warnings "))
    assert payload["warnings"]
    assert payload["warnings"][0]["allowlisted"] is True


def test_same_phrase_on_sot_with_empty_allowlist_blocks(monkeypatch):
    """SOT allowlist is empty — with compliance gate forced on, pressure still blocks."""
    cfg = tenant_config("salary_on_time").model_copy(
        update={"enforce_compliance_gate": True}
    )
    assert get_tenant_profile("salary_on_time").gate_allowlisted_phrases == []

    state = new_conversation_state("call-sot-al", "salary_on_time", "b1")
    state.slots["compliance_flags"] = {"dispute_hold": True}
    result = gate(
        "Please EMI jama karna hoga",
        state,
        cfg,
        now=_now("salary_on_time"),
    )
    assert result.verdict == "block"
    assert result.reason == "dispute_hold_no_pressure"
    assert result.text != "Please EMI jama karna hoga"


def test_partial_overlap_does_not_exempt_longer_violating_sentence():
    """Allowlist entry must appear in full; a pressure prefix alone does not exempt."""
    allowlist = ["payment kar sakte hain"]
    # Contains pressure "payment kar" + "due date" but NOT the full allowlisted phrase.
    text = "aaj payment kar warna due date tak"
    blocking, warnings = evaluate_pressure_with_allowlist(
        text,
        ["payment kar", "due date"],
        allowlist,
    )
    assert blocking is not None
    assert blocking in {"payment kar", "due date"}
    # No full allowlist hit → no allowlisted warnings for a covering phrase.
    assert not any(w.get("allowlisted_phrase") == allowlist[0] for w in warnings)


def test_partial_word_allowlist_does_not_match():
    """Allowlist match is word-bounded — 'pay' must not exempt inside 'payment'."""
    blocking, warnings = evaluate_pressure_with_allowlist(
        "please payment kar today",
        ["payment kar"],
        ["pay"],
    )
    assert blocking == "payment kar"
    assert warnings == []
