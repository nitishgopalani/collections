"""Unit tests for salary_on_time tenant config and per-tenant gate flags."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.config import get_settings, tenant_config
from app.engine.gate import gate
from app.engine.tracker import new_conversation_state


def _within_window_now(tenant_id: str = "default") -> datetime:
    cfg = tenant_config(tenant_id)
    return datetime(2026, 6, 25, 10, 0, tzinfo=ZoneInfo(cfg.call_window_timezone))


def _state(**flags):
    state = new_conversation_state("call-sot", "salary_on_time", "borrower-sot")
    state.slots["compliance_flags"] = flags
    return state


@pytest.mark.parametrize(
    ("tenant_id", "compliance_gate", "safety_gate"),
    [
        ("salary_on_time", False, True),
        ("default", True, True),
    ],
)
def test_tenant_gate_flags(tenant_id, compliance_gate, safety_gate):
    cfg = tenant_config(tenant_id)
    assert cfg.enforce_compliance_gate is compliance_gate
    assert cfg.enforce_safety_gate is safety_gate
    assert cfg.tenant_id == tenant_id


def test_pressure_line_blocked_under_default_tenant():
    state = new_conversation_state("call-c", "default", "borrower-c")
    state.slots["compliance_flags"] = {"dispute_hold": True}
    cfg = tenant_config("default")
    result = gate("Please EMI jama karna hoga", state, cfg, now=_within_window_now())
    assert result.verdict == "block"
    assert result.reason == "dispute_hold_no_pressure"


def test_pressure_line_passes_under_salary_on_time_tenant():
    state = _state(dispute_hold=True)
    cfg = tenant_config("salary_on_time")
    result = gate("Please EMI jama karna hoga", state, cfg, now=_within_window_now("salary_on_time"))
    assert result.verdict == "allow"
    assert result.reason == "ok"


def test_critical_inbound_still_blocked_under_salary_on_time():
    state = _state()
    cfg = tenant_config("salary_on_time")
    result = gate(
        "Namaste sir EMI ke liye call kar raha hoon",
        state,
        cfg,
        inbound_transcript="This is harassment complaint against you",
        now=_within_window_now("salary_on_time"),
    )
    assert result.verdict == "block"
    assert result.level == "CRITICAL"
    assert result.transfer_to_human is True


def test_test_mode_settings_defaults():
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.test_mode is False
    assert settings.test_tenant_id == "salary_on_time"
    assert settings.test_section == "pre_closure"
