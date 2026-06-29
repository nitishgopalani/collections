"""Simulator trace should emit turn_decision logs."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.sim.runner import run_sim_script


@pytest.mark.asyncio
async def test_dynamic_ptp_sim_emits_turn_decision_logs(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO, logger="app.engine.turn_decision_log")
    script = Path(__file__).resolve().parents[1] / "sim" / "dynamic_ptp.json"
    result = await run_sim_script(script)
    assert result.all_ok, result.issues
    decision_logs = [r for r in caplog.records if r.msg.startswith("turn_decision ")]
    assert len(decision_logs) >= 2
    combined = " ".join(r.msg for r in decision_logs)
    assert "identity_verification" in combined
    assert "promise_to_pay" in combined or "identity_response" in combined
