"""V2 / CP4 — end-to-end attempt-indexed objection → escalate END_CALL."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.nlg import REPLY_COUNTS_KEY
from app.engine.retrieval import clear_retrieval_cache
from app.engine.tenant_profile import clear_tenant_profile_cache
from app.engine.turn import handle_turn
from app.flows.loader import reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest

TRANSCRIPT_OUT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "_p4_v2_attempt_escalation_transcript.txt"
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("COLLECTIONS_INCLUDE_TEST_FLOWS", "true")
    monkeypatch.setenv("SCRIPTED_CATALOG_ROUTING", "true")
    monkeypatch.setenv("CALL_WINDOW_START", "00:00")
    monkeypatch.setenv("CALL_WINDOW_END", "23:59")
    # test_generic inherits default attempt caps; multi-turn digression needs headroom.
    from app.config import tenant_config as _real_tenant_config

    def _tenant_cfg(tenant_id: str):
        cfg = _real_tenant_config(tenant_id)
        if tenant_id == "test_generic":
            return cfg.model_copy(update={"max_attempts_per_day": 200})
        return cfg

    monkeypatch.setattr("app.engine.turn.tenant_config", _tenant_cfg)
    clear_tenant_profile_cache()
    get_settings.cache_clear()
    reload_flow_set()
    clear_retrieval_cache()
    yield
    clear_retrieval_cache()
    clear_tenant_profile_cache()
    get_settings.cache_clear()


class _EmptyKB:
    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def retrieve(self, text, tenant_id, k: int = 6):
        return []


class _ScriptedLLM:
    def __init__(self, turns):
        self._responses = [json.dumps(t) for t in turns]
        self.call_count = 0

    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def complete(self, system: str, user: str, *, json_only: bool = True, **kw) -> str:
        self.call_count += 1
        if self.call_count <= len(self._responses):
            return self._responses[self.call_count - 1]
        return "[]"


@pytest.mark.asyncio
async def test_objection_attempt_one_two_then_escalate_end_call():
    """Same objection ×1 → attempt-1, ×2 → attempt-2, ×3 → escalate_to END_CALL."""
    memory = InMemoryMemoryStore()
    call_id = "p4-v2-attempt-e2e"
    llm = _ScriptedLLM(
        [
            [],  # opener greeting
            # Keep a ladder frame alive (tg_ask) so force_end_no_flow does not fire;
            # objection digressions are started on subsequent turns.
            [
                {"command": "set_slot", "name": "tg_continue", "value": "yes"},
                {"command": "start_flow", "flow": "tg_ask"},
            ],
            [{"command": "start_flow", "flow": "tg_obj_repeat"}],
            [{"command": "start_flow", "flow": "tg_obj_repeat"}],
            [{"command": "start_flow", "flow": "tg_obj_repeat"}],
        ]
    )

    async def _run(transcript: str):
        return await handle_turn(
            TurnRequest(
                call_id=call_id,
                tenant_id="test_generic",
                borrower_id="tg_borrower",
                transcript=transcript,
                locale="en-IN",
                turn_meta={"force_flow": "tg_opener", "call_date": "2026-06-25"},
            ),
            memory=memory,
            kb=_EmptyKB(),
            llm=llm,
            tools=FakeToolClient(),
        )

    rows: list[dict[str, str]] = []
    turns = [
        ("", "t1"),
        ("yes continue", "t2"),
        ("same objection", "t3"),
        ("same objection again", "t4"),
        ("same objection third time", "t5"),
    ]
    for text, label in turns:
        resp = await _run(text)
        rows.append(
            {
                "turn": label,
                "borrower": text,
                "agent": resp.reply_text or "",
                "reply_id": resp.reply_id or "",
                "end_call": str(bool(resp.end_call)),
            }
        )

    # t3/t4/t5 are the objection plays (×1 / ×2 / ×3→escalate).
    assert "attempt one" in rows[2]["agent"].lower()
    assert rows[2]["reply_id"] == "tg_obj_repeat"
    assert rows[2]["end_call"] == "False"

    assert "attempt two" in rows[3]["agent"].lower()
    assert rows[3]["reply_id"] == "tg_obj_repeat"
    assert rows[3]["end_call"] == "False"

    assert "escalat" in rows[4]["agent"].lower()
    assert rows[4]["reply_id"] == "tg_obj_escalated"
    assert rows[4]["end_call"] == "True"

    state = await memory.load_state(call_id)
    assert state.slots.get("end_call") is True or state.slots.get("tg_call_closed")
    # Counters cleared on hangup / call closed.
    assert REPLY_COUNTS_KEY not in (state.slots or {})

    lines = [
        "# V2 — attempt-indexed objection e2e (test_generic / tg_obj_repeat)",
        "",
    ]
    for row in rows:
        lines.append(
            f"### {row['turn']} reply_id={row['reply_id']} end_call={row['end_call']}"
        )
        lines.append(f"Borrower: {row['borrower']!r}")
        lines.append(f"Agent: {row['agent']}")
        lines.append("")
    TRANSCRIPT_OUT.write_text("\n".join(lines), encoding="utf-8")
