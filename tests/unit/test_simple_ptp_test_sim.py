"""Tests for the simple_ptp_test flow simulator script."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.flows.loader import reload_flow_set
from app.sim.runner import load_sim_script, run_sim_script

SIM_PATH = Path(__file__).resolve().parent.parent / "sim" / "simple_ptp_test.json"


@pytest.fixture(autouse=True)
def _reload_flows():
    reload_flow_set()
    yield
    reload_flow_set()


@pytest.mark.asyncio
async def test_simple_ptp_test_happy_path(caplog):
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

    turn3 = result.traces[2]
    assert "send_payment_link" in turn3.actions_executed

    simulated_logs = [
        record.message
        for record in caplog.records
        if record.name == "app.clients.tools_sim" and "send_payment_link" in record.message
    ]
    assert simulated_logs, "expected simulated send_payment_link log on turn 3"
    payload = json.loads(simulated_logs[0])
    assert payload["status"] == "SIMULATED"
    assert payload["tool"] == "send_payment_link"
    assert payload["amount"] == 350
    assert "pay.example/test/sim-simple-ptp" in payload["link"]

    turn4 = result.traces[3]
    assert turn4.reply_id == "test_thanks"
    assert turn4.end_call is True
    assert "Dhanyavaad" in turn4.reply_text


@pytest.mark.asyncio
async def test_simple_ptp_test_exact_hindi_lines():
    script = load_sim_script(SIM_PATH)
    result = await run_sim_script(script)

    expected_text_snippets = [
        "Kya meri baat Rajesh ji se ho rahee hai",
        "350 rupaye due hai",
        "payment link WhatsApp par bhej raha hun",
        "Dhanyavaad! Aapka din shubh ho.",
    ]
    for trace, snippet in zip(result.traces, expected_text_snippets, strict=True):
        assert snippet in trace.reply_text
