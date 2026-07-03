"""Tests for per-turn routing decision logs."""

from __future__ import annotations

import json
import logging

import pytest

from app.engine.turn_decision_log import log_turn_decision
from app.schemas.command import Command
from app.schemas.state import BorrowerRecord, Frame
from app.engine.tracker import new_conversation_state


def test_log_turn_decision_emits_json(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO, logger="app.engine.turn_decision_log")
    state = new_conversation_state("call-1", "default", "B_RAJESH")
    state.flow_stack = [Frame(flow="identity_verification", step_index=0)]
    borrower = BorrowerRecord(
        borrower_id="B_RAJESH",
        identity={"name": "Rajesh"},
        loan={"amount_due": 350},
        comms_prefs={"language": "hi-IN", "phone": "+919810587857"},
    )
    log_turn_decision(
        session_id="call-1",
        transcript="mera naam Rajesh hai",
        borrower=borrower,
        kb_candidates=[{"name": "identity_verification", "score": 0.91}],
        commands=[
            Command(command="set_slot", name="identity_response", value="Rajesh"),
        ],
        rejected_slots=["rejected unknown slot borrower_name"],
        state=state,
        reply_id="ask_identity_verification",
        gate_verdict="allow",
        gate_reason=None,
        draft_reply="Namaste",
        final_reply="Namaste",
    )
    # The logger uses lazy %s-style args, so the fully-rendered line is getMessage(),
    # not .msg (which is only the "turn_decision %s" template).
    records = [r for r in caplog.records if r.getMessage().startswith("turn_decision ")]
    assert len(records) == 1
    payload = json.loads(records[0].getMessage().removeprefix("turn_decision "))
    assert payload["session_id"] == "call-1"
    assert payload["borrower"] == "B_RAJESH|Rajesh|350"
    assert payload["llm_start_flow"] == ""
    assert payload["active_flow"] == "identity_verification"
    assert payload["slots_set"]["identity_response"] == "Rajesh"
    assert "borrower_name" in payload["rejected_slots"][0]
    assert payload["reply_id"] == "ask_identity_verification"
    assert payload["gate"] == "allow"
