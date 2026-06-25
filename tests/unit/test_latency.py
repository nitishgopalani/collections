"""Latency profiler and Sprint 7 engine-internal budget tests."""

import statistics

import pytest

from app.clients.tools_sim import FakeToolClient
from app.engine.latency import StageTimer, TurnLatencyProfile
from app.engine.retrieval import clear_retrieval_cache
from app.engine.turn import handle_turn
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.schemas.state import BorrowerRecord
from tests.fixtures.test_borrowers import B_DUE
from tests.helpers.scripted_clients import ScriptedKB, ScriptedLLM

# Engine-internal p95 target (ms) — external KB/LLM/tool latency excluded.
ENGINE_INTERNAL_P95_TARGET_MS = 700.0


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_retrieval_cache()
    yield
    clear_retrieval_cache()


def test_turn_latency_profile_splits_internal_vs_external():
    profile = TurnLatencyProfile()
    profile.record("safety", 12.0)
    profile.record("retrieval", 120.0, external=True)
    profile.record("gate", 8.0)

    assert profile.engine_internal_ms == 20.0
    assert profile.external_ms == 120.0
    assert profile.total_ms == 140.0


def test_stage_timer_records_on_profile():
    profile = TurnLatencyProfile()
    with StageTimer(profile, "executor"):
        pass
    assert "executor" in profile.stage_ms()


@pytest.mark.asyncio
async def test_handle_turn_engine_internal_p95_budget():
    """Stub clients — engine-internal p95 should meet ≤ 700 ms target."""
    memory = InMemoryMemoryStore()
    kb = ScriptedKB(
        [{"doc_id": "1", "score": 0.9, "text": "[[flow:promise_to_pay]] kal"}]
    )
    llm = ScriptedLLM(
        [
            [
                {"command": "start_flow", "flow": "promise_to_pay"},
                {"command": "set_slot", "name": "ptp_date", "value": "2026-06-27"},
            ]
        ]
    )
    tools = FakeToolClient()
    tools.reset()
    await memory.save_borrower(
        BorrowerRecord(
            borrower_id=B_DUE,
            loan={"amount_due": 5000, "dpd": 45, "bucket": "30-60"},
            identity={"identity_ok": True},
        )
    )

    internal_samples: list[float] = []
    for index in range(30):
        call_id = f"latency-{index}"
        response = await handle_turn(
            TurnRequest(
                call_id=call_id,
                tenant_id="default",
                borrower_id=B_DUE,
                transcript=f"kal de dunga turn {index}",
                turn_meta={"call_date": "2026-06-25"},
            ),
            memory=memory,
            kb=kb,
            llm=llm,
            tools=tools,
        )
        audits = await memory.list_audit(B_DUE)
        last = audits[-1]
        from app.memory.audit import parse_turn_audit_chains

        chain = parse_turn_audit_chains([last])[0]
        internal_samples.append(chain.engine_internal_ms)
        assert response.reply_text

    p95 = statistics.quantiles(internal_samples, n=20)[18]  # 95th percentile
    assert p95 <= ENGINE_INTERNAL_P95_TARGET_MS, (
        f"engine-internal p95 {p95:.1f} ms exceeds {ENGINE_INTERNAL_P95_TARGET_MS} ms "
        f"(samples={internal_samples})"
    )
