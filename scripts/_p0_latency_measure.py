"""Measure command_gen + retrieval stage ms on the SOT golden happy path."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys

os.environ.setdefault("STUB_MODE", "true")
os.environ.setdefault("KB_STUB", "true")
os.environ.setdefault("TOOLS_STUB", "true")
os.environ.setdefault("LLM_STUB", "true")
os.environ.setdefault("ORCHESTRATOR_BASE_URL", "")
os.environ.setdefault("WHATSAPP_MODE", "stub")
os.environ.setdefault("SOT_DIGRESSION", "false")
os.environ.setdefault("TEST_MODE", "true")
os.environ.setdefault("CALL_WINDOW_START", "00:00")
os.environ.setdefault("CALL_WINDOW_END", "23:59")


async def main(label: str) -> None:
    from app.config import get_settings

    get_settings.cache_clear()
    from app.clients.tools_sim import FakeToolClient
    from app.engine.retrieval import clear_retrieval_cache
    from app.engine.turn import handle_turn
    from app.flows.loader import reload_flow_set
    from app.memory.audit import parse_turn_audit_chains
    from app.memory.store import InMemoryMemoryStore
    from app.schemas.api import TurnRequest

    reload_flow_set()
    clear_retrieval_cache()

    class ScriptedKBEmpty:
        @property
        def is_stub(self):
            return False

        async def ping(self):
            return True

        async def retrieve(self, text, tenant_id, k=6):
            return []

    class ScriptedLLM:
        def __init__(self, turns):
            self._responses = [json.dumps(t) for t in turns]
            self.call_count = 0

        @property
        def is_stub(self):
            return False

        async def ping(self):
            return True

        async def complete(self, system, user, *, json_only=True, **kw):
            self.call_count += 1
            if self.call_count <= len(self._responses):
                return self._responses[self.call_count - 1]
            return "[]"

    memory = InMemoryMemoryStore()
    call_id = f"sot-latency-{label.lower()}"
    turns = [
        [],
        [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
        [
            {"command": "set_slot", "name": "sot_payment_intent", "value": "willing"},
            {"command": "set_slot", "name": "sot_commit_timing", "value": "today"},
        ],
        [{"command": "set_slot", "name": "sot_customer_time", "value": "shaam 5 baje"}],
        [{"command": "set_slot", "name": "sot_final_confirm", "value": "yes"}],
    ]
    llm = ScriptedLLM(turns)
    transcripts = [
        "",
        "haan main hi bol raha hoon",
        "haan aaj kar dunga",
        "shaam 5 baje",
        "haan confirm",
    ]
    cg: list[float] = []
    ret: list[float] = []
    for i, tr in enumerate(transcripts):
        await handle_turn(
            TurnRequest(
                call_id=call_id,
                tenant_id="salary_on_time",
                borrower_id="sot_test_borrower",
                transcript=tr,
                turn_meta={"force_flow": "sot_opener", "call_date": "2026-06-25"},
            ),
            memory=memory,
            kb=ScriptedKBEmpty(),
            llm=llm,
            tools=FakeToolClient(),
        )
        audits = await memory.list_audit("sot_test_borrower")
        chain = parse_turn_audit_chains(audits)[-1]
        stages = chain.latency_ms or {}
        cg.append(float(stages.get("command_gen", 0.0)))
        ret.append(float(stages.get("retrieval", 0.0)))
        print(
            f"T{i + 1}: command_gen={stages.get('command_gen')} "
            f"retrieval={stages.get('retrieval')} "
            f"internal={chain.engine_internal_ms} external={chain.external_ms}"
        )
    print(f"{label} summary:")
    print(
        f"  command_gen: sum={sum(cg):.2f} mean={statistics.mean(cg):.2f} "
        f"p50={statistics.median(cg):.2f} max={max(cg):.2f}"
    )
    print(
        f"  retrieval:   sum={sum(ret):.2f} mean={statistics.mean(ret):.2f} "
        f"p50={statistics.median(ret):.2f} max={max(ret):.2f}"
    )
    print(
        "RAW_JSON",
        json.dumps({"label": label, "command_gen": cg, "retrieval": ret}),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="BEFORE")
    args = parser.parse_args()
    asyncio.run(main(args.label))
