"""Tests for identity_name_confirm flow simulator script."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.flows.loader import reload_flow_set
from app.sim.runner import load_sim_script, run_sim_script

SIM_PATH = Path(__file__).resolve().parent.parent / "sim" / "identity_name_confirm.json"


@pytest.fixture(autouse=True)
def _reload_flows():
    reload_flow_set()
    yield
    reload_flow_set()


@pytest.mark.asyncio
async def test_identity_name_confirm_happy_path(caplog):
    caplog.set_level(logging.INFO, logger="app.clients.tools_sim")
    script = load_sim_script(SIM_PATH)
    result = await run_sim_script(script)

    assert result.all_ok, result.issues
    assert len(result.traces) == 4

    expected_reply_ids = script["expect"]["reply_ids"]
    for trace, expected_id in zip(result.traces, expected_reply_ids, strict=True):
        assert trace.reply_id == expected_id
        assert trace.gate_verdict == "allow"
        assert trace.reply_text.strip()

    opener = result.traces[0]
    assert "Rajesh" in opener.reply_text

    payment = result.traces[1]
    assert "due hai" in payment.reply_text
    assert "rupaye" in payment.reply_text

    link_turn = result.traces[2]
    assert "send_payment_link" in link_turn.actions_executed
    simulated_logs = [
        record.message
        for record in caplog.records
        if record.name == "app.clients.tools_sim" and "send_payment_link" in record.message
    ]
    assert simulated_logs
    payload = json.loads(simulated_logs[0])
    assert payload["status"] == "SIMULATED"
    assert payload["amount"] == 350

    closing = result.traces[3]
    assert closing.end_call is True
    assert "Dhanyavaad" in closing.reply_text


def test_identity_confirmed_slot_accepted_by_command_gen():
    from app.engine.command_gen import parse_and_validate_commands

    parsed = parse_and_validate_commands(
        '[{"command":"set_slot","name":"identity_confirmed","value":"confirmed"}]'
    )
    assert len(parsed.commands) == 1
    assert parsed.commands[0].name == "identity_confirmed"
    assert parsed.commands[0].value == "confirmed"
