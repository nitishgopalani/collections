#!/usr/bin/env python3
"""Run dynamic-routing sim scripts (PTP / dispute / hardship) and print flow + slot traces."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.sim.runner import format_sim_transcript, load_sim_script, run_sim_script  # noqa: E402

SCRIPTS = [
    ROOT / "tests" / "sim" / "dynamic_ptp.json",
    ROOT / "tests" / "sim" / "dynamic_dispute.json",
    ROOT / "tests" / "sim" / "dynamic_hardship.json",
]


async def run_one(path: Path) -> int:
    script = load_sim_script(path)
    result = await run_sim_script(script)
    print(format_sim_transcript(result))
    print(f"scenario={result.name} all_ok={result.all_ok}")
    for trace in result.traces:
        print(
            f"  turn {trace.turn_index}: flow={trace.active_flow} "
            f"slots_set={json.dumps(trace.slots_set, ensure_ascii=False)} "
            f"gate={trace.gate_verdict}"
        )
    if result.issues:
        for issue in result.issues:
            print(f"  ISSUE: {issue}")
    print()
    return 0 if result.all_ok else 1


async def main() -> int:
    code = 0
    for path in SCRIPTS:
        if not path.is_file():
            print(f"missing script: {path}")
            return 1
        code |= await run_one(path)
    return code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
