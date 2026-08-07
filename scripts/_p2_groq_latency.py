"""P2.4 — real Groq command_gen latency on the 5-turn SOT golden (3 runs).

Usage:
  python scripts/_p2_groq_latency.py --label before   # digression/RAG path
  python scripts/_p2_groq_latency.py --label after    # catalog path (default)

Does not print secrets. Requires GROQ_API_KEY in env or .env.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# Load .env before settings (pydantic also loads it; this ensures subprocess env).
_env = REPO / ".env"
if _env.is_file():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

os.environ["STUB_MODE"] = "true"
os.environ["KB_STUB"] = "true"
os.environ["TOOLS_STUB"] = "true"
os.environ["LLM_STUB"] = "false"
os.environ["LLM_PROVIDER"] = "groq"
os.environ["ORCHESTRATOR_BASE_URL"] = ""
os.environ["WHATSAPP_MODE"] = "stub"
os.environ["TEST_MODE"] = "true"
os.environ["CALL_WINDOW_START"] = "00:00"
os.environ["CALL_WINDOW_END"] = "23:59"
os.environ.setdefault("COLLECTIONS_INCLUDE_TEST_FLOWS", "false")


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    ordered = sorted(xs)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(ordered) - 1)
    if f == c:
        return ordered[f]
    return ordered[f] + (ordered[c] - ordered[f]) * (k - f)


async def _one_run(run_idx: int, label: str) -> list[float]:
    from app.config import get_settings
    from app.clients.llm_vertex import create_llm_client
    from app.clients.tools_sim import FakeToolClient
    from app.engine.retrieval import clear_retrieval_cache
    from app.engine.turn import handle_turn
    from app.flows.loader import reload_flow_set
    from app.memory.audit import parse_turn_audit_chains
    from app.memory.store import InMemoryMemoryStore
    from app.schemas.api import TurnRequest

    get_settings.cache_clear()
    reload_flow_set()
    clear_retrieval_cache()
    llm = create_llm_client()
    if getattr(llm, "is_stub", False):
        raise SystemExit("LLM client is stub — set GROQ_API_KEY and LLM_STUB=false")

    class _EmptyKB:
        @property
        def is_stub(self):
            return False

        async def ping(self):
            return True

        async def retrieve(self, text, tenant_id, k=6):
            return []

    memory = InMemoryMemoryStore()
    call_id = f"p2-lat-{label}-r{run_idx}"
    transcripts = [
        "",
        "haan main hi bol raha hoon",
        "haan aaj kar dunga",
        "shaam 5 baje",
        "haan confirm",
    ]
    cg: list[float] = []
    for tr in transcripts:
        await handle_turn(
            TurnRequest(
                call_id=call_id,
                tenant_id="salary_on_time",
                borrower_id="sot_test_borrower",
                transcript=tr,
                turn_meta={"force_flow": "sot_opener", "call_date": "2026-06-25"},
            ),
            memory=memory,
            kb=_EmptyKB(),
            llm=llm,
            tools=FakeToolClient(),
        )
        audits = await memory.list_audit("sot_test_borrower")
        chain = parse_turn_audit_chains(audits)[-1]
        stages = chain.latency_ms or {}
        cg.append(float(stages.get("command_gen", 0.0)))
    return cg


async def main(label: str, runs: int, digression: bool) -> None:
    os.environ["SOT_DIGRESSION"] = "true" if digression else "false"
    # Optional kill-switch used after P2 lands (ignored before catalog exists).
    if label == "before":
        os.environ["SCRIPTED_CATALOG_ROUTING"] = "false"
    else:
        os.environ["SCRIPTED_CATALOG_ROUTING"] = "true"

    all_cg: list[float] = []
    per_run: list[list[float]] = []
    for i in range(runs):
        print(f"=== run {i + 1}/{runs} label={label} digression={digression} ===")
        cg = await _one_run(i, label)
        per_run.append(cg)
        all_cg.extend(cg)
        print("  turns ms:", ", ".join(f"{x:.1f}" for x in cg))

    print()
    print(f"LABEL={label} digression={digression} runs={runs} turns={len(all_cg)}")
    print(
        f"command_gen p50={_pct(all_cg, 50):.1f} p95={_pct(all_cg, 95):.1f} "
        f"mean={statistics.mean(all_cg):.1f} max={max(all_cg):.1f}"
    )
    out = REPO / "scripts" / f"_p2_latency_{label}.txt"
    out.write_text(
        f"label={label}\ndigression={digression}\nruns={runs}\n"
        f"per_run={per_run!r}\n"
        f"p50={_pct(all_cg, 50):.1f}\np95={_pct(all_cg, 95):.1f}\n"
        f"mean={statistics.mean(all_cg):.1f}\nmax={max(all_cg):.1f}\n",
        encoding="utf-8",
    )
    print(f"wrote {out}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--label", default="after", choices=["before", "after"])
    p.add_argument("--runs", type=int, default=3)
    p.add_argument(
        "--digression",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="SOT_DIGRESSION for the before/legacy path (default true)",
    )
    args = p.parse_args()
    asyncio.run(main(args.label, args.runs, args.digression))
