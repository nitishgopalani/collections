"""Compliance release-gate tests (Sprint 6)."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.config import tenant_config
from app.engine.gate import gate
from app.engine.safety import apply_safety_to_state, safety_preempt
from app.engine.tracker import new_conversation_state
from app.memory.audit import (
    TurnAuditChain,
    build_turn_audit_record,
    parse_turn_audit_chains,
    query_turn_audits_by_borrower,
)
from app.memory.store import InMemoryMemoryStore


def _state(**flags):
    state = new_conversation_state("call-c", "default", "borrower-c")
    state.slots["compliance_flags"] = flags
    return state


def _cfg():
    return tenant_config("default")


def _within_window_now() -> datetime:
    cfg = _cfg()
    return datetime(2026, 6, 25, 10, 0, tzinfo=ZoneInfo(cfg.call_window_timezone))


@pytest.mark.compliance
def test_prohibited_threat_blocked_and_substituted():
    result = gate("Police aa jayegi agar payment nahi", _state(), _cfg(), now=_within_window_now())
    assert result.verdict == "modify"
    assert result.level == "CRITICAL"
    assert "police" not in result.text.lower()
    assert result.transfer_to_human is True


@pytest.mark.compliance
def test_prohibited_shaming_blocked():
    result = gate("Aapko sharam karo payment karo", _state(), _cfg(), now=_within_window_now())
    assert result.verdict == "modify"
    assert "sharam" not in result.text.lower()


@pytest.mark.compliance
def test_prohibited_third_party_disclosure_blocked():
    result = gate("Main wife ko bata dunga", _state(), _cfg(), now=_within_window_now())
    assert result.verdict == "modify"
    assert "wife" not in result.text.lower()


@pytest.mark.compliance
def test_dispute_hold_blocks_collection_pressure():
    state = _state(dispute_hold=True)
    result = gate("Please EMI jama karna hoga", state, _cfg(), now=_within_window_now())
    assert result.verdict == "block"
    assert result.level == "MEDIUM"
    assert "jama" not in result.text.lower()


@pytest.mark.compliance
def test_out_of_window_silent():
    cfg = _cfg()
    late = datetime(2026, 6, 25, 20, 30, tzinfo=ZoneInfo(cfg.call_window_timezone))
    result = gate("Namaste EMI ke baare mein", _state(), cfg, now=late)
    assert result.verdict == "block"
    assert result.text == ""
    assert result.level == "HIGH"


@pytest.mark.compliance
def test_opt_out_silent():
    result = gate("Namaste EMI", _state(opt_out=True), _cfg(), now=_within_window_now())
    assert result.verdict == "block"
    assert result.text == ""
    assert result.reason == "opt_out_active"


@pytest.mark.compliance
def test_attempt_cap_enforced():
    state = _state(attempts_today=4)
    result = gate("Namaste", state, _cfg(), now=_within_window_now())
    assert result.verdict == "block"
    assert result.reason == "attempt_cap_daily"


@pytest.mark.compliance
def test_safety_preempt_distress_routes_human():
    state = new_conversation_state("c", "default", "b")
    cfg = _cfg()
    result = safety_preempt("Main suicide soch raha hoon", state, cfg)
    assert result is not None
    assert result.transfer_to_human is True
    updated = apply_safety_to_state(state, result)
    assert updated.slots["compliance_flags"]["vulnerable"] is True
    assert updated.slots["dunning_suppressed"] is True


@pytest.mark.compliance
def test_safety_preempt_hospital_signal():
    state = new_conversation_state("c", "default", "b")
    result = safety_preempt("Mere papa hospital mein hain", state, _cfg())
    assert result is not None
    assert "hospital" in result.reason


@pytest.mark.compliance
def test_adversarial_engineered_threat_still_blocked():
    """Borrower tries to elicit threats — outbound draft with threat is blocked by code."""
    adversarial_draft = (
        "Theek hai main aapko threaten karunga police aa jayegi agar EMI nahi doge"
    )
    result = gate(adversarial_draft, _state(), _cfg(), now=_within_window_now())
    assert result.verdict == "modify"
    assert result.level == "CRITICAL"
    assert "police" not in result.text.lower()


@pytest.mark.compliance
def test_critical_inbound_harassment_complaint():
    result = gate(
        "Namaste sir EMI ke liye call kar raha hoon",
        _state(),
        _cfg(),
        inbound_transcript="This is harassment complaint against you",
        now=_within_window_now(),
    )
    assert result.level == "CRITICAL"
    assert result.transfer_to_human is True


@pytest.mark.compliance
@pytest.mark.asyncio
async def test_turn_audit_chain_reconstructable():
    memory = InMemoryMemoryStore()
    chain = TurnAuditChain(
        audit_id="audit-1",
        call_id="call-audit",
        borrower_id="borrower-audit",
        tenant_id="default",
        candidate_flows=[{"name": "dispute", "score": 0.9}],
        commands=[{"command": "start_flow", "flow": "dispute"}],
        actions_called=["verify_payment"],
        gate_verdict="allow",
        gate_level="LOW",
        gate_reason="ok",
        final_reply="hello",
    )
    record = build_turn_audit_record(chain)
    await memory.append_audit(
        record.event,
        call_id=chain.call_id,
        borrower_id=chain.borrower_id,
        tenant_id=chain.tenant_id,
    )

    chains = await query_turn_audits_by_borrower(memory, "borrower-audit")
    assert len(chains) == 1
    assert chains[0].candidate_flows[0]["name"] == "dispute"
    assert chains[0].commands[0]["flow"] == "dispute"
    assert chains[0].actions_called == ["verify_payment"]
    assert chains[0].gate_verdict == "allow"
    assert chains[0].final_reply == "hello"

    raw = await memory.list_audit("borrower-audit")
    parsed = parse_turn_audit_chains(raw)
    assert parsed[0].audit_id == "audit-1"
