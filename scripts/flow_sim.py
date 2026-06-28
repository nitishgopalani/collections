#!/usr/bin/env python3
"""CLI flow simulator — drive handle_turn without audio or telephony."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.sim.runner import (  # noqa: E402
    SimTurnSpec,
    format_sim_transcript,
    load_sim_script,
    parse_gate_now,
    run_sim_script,
    simulate_conversation,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Simulate collections brain turns locally (no audio, no telephony)."
    )
    parser.add_argument(
        "--script",
        type=Path,
        help="JSON conversation script (see tests/sim/*.json)",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="REPL mode: type borrower lines; Ctrl-D or 'quit' to exit",
    )
    parser.add_argument("--call-id", default="sim-local")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--borrower-id", default="B_DUE")
    parser.add_argument("--agent-id", default="agent-1")
    parser.add_argument("--locale", default="hi-IN")
    parser.add_argument(
        "--call-date",
        help="ISO date for relative-date resolution and stable gate clock (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--gate-now",
        help="Override compliance gate clock (ISO-8601, e.g. 2026-06-25T20:30:00+05:30)",
    )
    parser.add_argument(
        "--borrower-fixture",
        help="Borrower fixture id (B_DUE, B_VERIFY_OK, ...)",
    )
    parser.add_argument(
        "--live-llm",
        action="store_true",
        help="Use real Vertex command-gen (requires GCP creds; default uses scripted/stub LLM)",
    )
    parser.add_argument(
        "--json-out",
        action="store_true",
        help="Print machine-readable trace JSON instead of annotated transcript",
    )
    return parser


async def _run_scripted(args: argparse.Namespace) -> int:
    script = load_sim_script(args.script)
    gate_now = parse_gate_now(args.gate_now) if args.gate_now else None
    result = await run_sim_script(
        script,
        gate_now_override=gate_now,
        use_live_llm=args.live_llm,
    )
    if args.json_out:
        payload = {
            "name": result.name,
            "call_id": result.call_id,
            "borrower_id": result.borrower_id,
            "gate_now": result.gate_now.isoformat() if result.gate_now else None,
            "all_ok": result.all_ok,
            "issues": result.issues,
            "traces": [
                {
                    "turn_index": trace.turn_index,
                    "label": trace.label,
                    "borrower_text": trace.borrower_text,
                    "active_flow": trace.active_flow,
                    "reply_id": trace.reply_id,
                    "variant_index": trace.variant_index,
                    "reply_text": trace.reply_text,
                    "gate_verdict": trace.gate_verdict,
                    "gate_reason": trace.gate_reason,
                    "slots_set": trace.slots_set,
                    "actions_executed": trace.actions_executed,
                    "ok": trace.ok,
                    "issue": trace.issue,
                }
                for trace in result.traces
            ],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(format_sim_transcript(result))
    return 0 if result.all_ok else 1


async def _run_interactive(args: argparse.Namespace) -> int:
    print("Interactive flow sim (empty line = opener turn). Type 'quit' to exit.")
    turns: list[SimTurnSpec] = []
    while True:
        try:
            line = input("borrower> ")
        except EOFError:
            break
        if line.strip().lower() in {"quit", "exit"}:
            break
        turns.append(
            SimTurnSpec(
                transcript=line,
                label=f"line-{len(turns) + 1}",
                opener=not line.strip() and len(turns) == 0,
            )
        )

    if not turns:
        print("No turns entered.")
        return 1

    gate_now = parse_gate_now(args.gate_now) if args.gate_now else None
    result = await simulate_conversation(
        name="interactive",
        call_id=args.call_id,
        tenant_id=args.tenant_id,
        borrower_id=args.borrower_id,
        turns=turns,
        borrower_spec={"borrower_fixture": args.borrower_fixture or "B_DUE"},
        agent_id=args.agent_id,
        locale=args.locale,
        call_date=args.call_date or "2026-06-25",
        gate_now=gate_now,
        use_live_llm=args.live_llm,
    )
    print(format_sim_transcript(result))
    return 0 if result.all_ok else 1


async def _async_main(args: argparse.Namespace) -> int:
    if args.script:
        return await _run_scripted(args)
    if args.interactive:
        return await _run_interactive(args)
    print("Provide --script PATH or --interactive", file=sys.stderr)
    return 2


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
