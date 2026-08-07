"""P6.1 — Replay harness: JSONL turns through handle_turn; routing + latency.

Seed: session 5f001c27 t1–t7 + handcrafted OOS utterances.

Usage:
    cd Collection
    py -3 scripts/replay_eval.py
    py -3 scripts/replay_eval.py --seed scripts/_p6_replay_seed.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.retrieval import clear_retrieval_cache
from app.engine.tenant_profile import clear_tenant_profile_cache
from app.engine.turn import handle_turn
from app.flows.loader import reload_flow_set
from app.engine.tracker import new_conversation_state
from app.memory.store import InMemoryMemoryStore
from app.memory.test_borrower import apply_test_borrower_slots, hardcoded_test_borrower
from app.schemas.api import TurnRequest
from app.schemas.state import Frame
from app.sim.scripted_clients import ScriptedKB, ScriptedLLM

DEFAULT_SEED = ROOT / "_p6_replay_seed.jsonl"
REPORT_PATH = ROOT / "_p6_replay_report.json"


class _LatencyKB(ScriptedKB):
    pass


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="P6.1 replay eval harness")
    p.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    p.add_argument("--report", type=Path, default=REPORT_PATH)
    p.add_argument("--min-accuracy", type=float, default=0.90)
    return p.parse_args()


def _load_seed(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rows.append(json.loads(line))
    return rows


def _match_expected(resp, state, expected: dict[str, Any]) -> tuple[bool, str]:
    if not expected:
        return True, "no_expect"
    if "reply_id" in expected:
        if resp.reply_id != expected["reply_id"]:
            return False, f"reply_id got={resp.reply_id!r} want={expected['reply_id']!r}"
    if "flow" in expected and state is not None:
        flows = [f.flow for f in state.flow_stack]
        want = expected["flow"]
        if want not in flows and (not flows or flows[-1] != want):
            # Also accept reply_id-derived soft match when stack empty after close.
            if expected.get("allow_empty_stack") and not flows:
                return True, "ok_empty_stack"
            return False, f"flow got={flows!r} want={want!r}"
    if "slot" in expected and state is not None:
        name = expected["slot"]
        if name not in state.slots or state.slots.get(name) in (None, ""):
            return False, f"slot missing={name!r}"
    if expected.get("no_escalated_unclear"):
        if state and state.slots.get("disposition") == "ESCALATED_UNCLEAR":
            return False, "ESCALATED_UNCLEAR"
    if "text_contains" in expected:
        if expected["text_contains"] not in (resp.reply_text or ""):
            return False, "text_contains miss"
    return True, "ok"


async def _bootstrap_mid_push(memory: InMemoryMemoryStore, call_id: str, borrower_id: str) -> None:
    """Park a call mid sot_push collect so OOS/info turns exercise Tier-3 respond."""
    state = new_conversation_state(call_id, "salary_on_time", borrower_id)
    borrower = hardcoded_test_borrower(borrower_id, scenario="pre")
    state = apply_test_borrower_slots(state, borrower)
    state.slots["identity_ok"] = True
    state.slots["call_date"] = "2026-08-06"
    state.flow_stack = [Frame(flow="sot_push", step_index=0)]
    # First save must be version 1 (store expects previous == 0).
    state.version = 1
    await memory.save_state(state)


async def _run_session(rows: list[dict[str, Any]]) -> dict[str, Any]:
    clear_retrieval_cache()
    clear_tenant_profile_cache()
    get_settings.cache_clear()
    reload_flow_set()

    memory = InMemoryMemoryStore()
    tenant_id = "salary_on_time"
    borrower = hardcoded_test_borrower("replay_borrower", scenario="pre")
    await memory.save_borrower(borrower)

    llm_turns: list[list[dict[str, Any]]] = []
    for row in rows:
        cmds = row.get("llm_commands")
        llm_turns.append(list(cmds) if isinstance(cmds, list) else [])
    llm = ScriptedLLM(llm_turns)
    kb = _LatencyKB([])
    tools = FakeToolClient()

    results: list[dict[str, Any]] = []
    hits = 0
    info_q_escalations = 0
    latencies: list[float] = []
    bootstrapped: set[str] = set()

    for i, row in enumerate(rows, start=1):
        call_id = str(row.get("call_id") or "replay-sess")
        transcript = row.get("transcript", "")
        expected = row.get("expected") or {}
        meta = dict(row.get("turn_meta") or {})
        meta.setdefault("call_date", "2026-08-06")
        if row.get("bootstrap") == "mid_push" and call_id not in bootstrapped:
            await _bootstrap_mid_push(memory, call_id, borrower.borrower_id)
            bootstrapped.add(call_id)
            meta.setdefault("force_flow", "sot_push")
        elif i == 1 and "force_flow" not in meta:
            meta["force_flow"] = "sot_opener"

        req = TurnRequest(
            call_id=call_id,
            tenant_id=tenant_id,
            borrower_id=borrower.borrower_id,
            channel="voice",
            locale="hi-IN",
            transcript=transcript,
            turn_meta=meta,
        )
        t0 = time.perf_counter()
        resp = await handle_turn(req, memory=memory, llm=llm, tools=tools, kb=kb)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        latencies.append(elapsed_ms)
        state = await memory.load_state(call_id)
        ok, reason = _match_expected(resp, state, expected)
        if ok:
            hits += 1
        if row.get("kind") == "info_question" and state and state.slots.get(
            "disposition"
        ) == "ESCALATED_UNCLEAR":
            info_q_escalations += 1
        results.append(
            {
                "i": i,
                "call_id": call_id,
                "transcript": transcript,
                "kind": row.get("kind"),
                "reply_id": resp.reply_id,
                "latency_ms": round(elapsed_ms, 2),
                "ok": ok,
                "reason": reason,
                "flows": [f.flow for f in (state.flow_stack if state else [])],
            }
        )

    accuracy = hits / max(len(rows), 1)
    return {
        "n": len(rows),
        "hits": hits,
        "accuracy": round(accuracy, 4),
        "info_question_escalated_unclear": info_q_escalations,
        "latency_ms_avg": round(sum(latencies) / max(len(latencies), 1), 2),
        "latency_ms_p50": round(sorted(latencies)[len(latencies) // 2], 2)
        if latencies
        else 0,
        "turns": results,
    }


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    args = _parse_args()
    if not args.seed.is_file():
        print(f"missing seed: {args.seed}")
        return 2
    rows = _load_seed(args.seed)
    report = asyncio.run(_run_session(rows))
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"replay_eval accuracy={report['accuracy']:.1%} "
        f"({report['hits']}/{report['n']}) "
        f"avg_ms={report['latency_ms_avg']} "
        f"info_escalations={report['info_question_escalated_unclear']}"
    )
    print(f"report: {args.report}")
    if report["accuracy"] < args.min_accuracy:
        print(f"FAIL: accuracy < {args.min_accuracy}")
        return 1
    if report["info_question_escalated_unclear"] > 0:
        print("FAIL: ESCALATED_UNCLEAR on info-question turns")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
