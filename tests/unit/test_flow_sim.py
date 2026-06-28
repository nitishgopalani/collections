"""Pytest harness for JSON flow simulation scripts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.sim.runner import load_sim_script, run_sim_script

SIM_DIR = Path(__file__).resolve().parent.parent / "sim"
SIM_SCRIPTS = sorted(SIM_DIR.glob("*.json"))


def _assert_turn_expect(trace, expect: dict) -> None:
    if "gate_verdict" in expect:
        assert trace.gate_verdict == expect["gate_verdict"]
    if "gate_reason" in expect:
        assert trace.gate_reason == expect["gate_reason"]
    if expect.get("reply_text_empty"):
        assert not trace.reply_text.strip()
    if "min_reply_len" in expect:
        assert len(trace.reply_text.strip()) >= int(expect["min_reply_len"])
    for action in expect.get("actions_include") or []:
        assert action in trace.actions_executed


@pytest.mark.parametrize("script_path", SIM_SCRIPTS, ids=lambda p: p.stem)
@pytest.mark.asyncio
async def test_sim_script_runs(script_path: Path):
    script = load_sim_script(script_path)
    result = await run_sim_script(script)
    assert result.all_ok, result.issues

    expect_root = script.get("expect") or {}
    if expect_root.get("all_ok") is False:
        assert not result.all_ok
        return

    turn_expects = expect_root.get("turns") or []
    assert len(result.traces) == len(script["turns"])
    for trace, turn_expect in zip(result.traces, turn_expects, strict=False):
        if turn_expect:
            _assert_turn_expect(trace, turn_expect)


@pytest.mark.asyncio
async def test_after_hours_is_deliberate_silent():
    script = load_sim_script(SIM_DIR / "after_hours.json")
    result = await run_sim_script(script)
    trace = result.traces[0]
    assert trace.gate_reason == "outside_call_window"
    assert not trace.reply_text.strip()
    assert trace.ok


def test_sim_scripts_are_valid_json():
    for path in SIM_SCRIPTS:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        assert "turns" in data
        assert "call_id" in data
        assert "borrower_id" in data
