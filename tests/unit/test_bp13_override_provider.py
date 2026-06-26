"""BP-1.3: OverrideProvider seam + pack resolution (not merged into reply path)."""

import pytest

from app.clients.tools_sim import FakeToolClient
from app.engine.turn import handle_turn
from app.flows.loader import load_all_flows
from app.flows.manifest import MANIFEST_VERSION
from app.flows.override_provider import FixtureOverrideProvider
from app.memory.audit import query_turn_audits_by_borrower
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.schemas.state import BorrowerRecord
from tests.fixtures.brand_override_packs import (
    FIXTURE_PACKS_BY_AGENT,
    PACK_APPEND_MINIMAL,
    PACK_HEAVIER_REPLACE,
)
from tests.helpers.scripted_clients import ScriptedKB, ScriptedLLM

FLOWS = load_all_flows()
FIXTURE_PROVIDER = FixtureOverrideProvider(FIXTURE_PACKS_BY_AGENT)


async def _seed_verified_borrower(memory: InMemoryMemoryStore, borrower_id: str) -> None:
    await memory.save_borrower(
        BorrowerRecord(
            borrower_id=borrower_id,
            loan={"amount_due": 5000, "dpd": 45, "bucket": "30-60"},
            identity={"identity_ok": True},
        )
    )


def _scripted_ptp_clients() -> tuple[ScriptedKB, ScriptedLLM, FakeToolClient]:
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:promise_to_pay]] kal"}])
    llm = ScriptedLLM(
        [
            [
                {"command": "start_flow", "flow": "promise_to_pay"},
                {"command": "set_slot", "name": "ptp_date", "value": "2026-06-27"},
            ]
        ]
    )
    return kb, llm, FakeToolClient()


@pytest.mark.asyncio
async def test_fixture_provider_returns_pack_by_agent_id():
    pack = await FIXTURE_PROVIDER.get_pack(
        agent_id=PACK_APPEND_MINIMAL.agent_id,
        pack_id=PACK_APPEND_MINIMAL.pack_id,
    )
    assert pack is PACK_APPEND_MINIMAL

    heavy = await FIXTURE_PROVIDER.get_pack(
        agent_id=PACK_HEAVIER_REPLACE.agent_id,
        pack_id=None,
    )
    assert heavy is PACK_HEAVIER_REPLACE


@pytest.mark.asyncio
async def test_fixture_provider_returns_none_for_unknown_or_missing_agent():
    assert await FIXTURE_PROVIDER.get_pack(agent_id=None, pack_id="pack-append-v1") is None
    assert await FIXTURE_PROVIDER.get_pack(agent_id="unknown-agent", pack_id=None) is None
    assert (
        await FIXTURE_PROVIDER.get_pack(
            agent_id=PACK_APPEND_MINIMAL.agent_id,
            pack_id="wrong-pack-id",
        )
        is None
    )


@pytest.mark.asyncio
async def test_handle_turn_records_resolved_pack_in_audit():
    memory = InMemoryMemoryStore()
    kb, llm, tools = _scripted_ptp_clients()
    await _seed_verified_borrower(memory, "borrower-bp13-with-pack")

    with_pack_request = TurnRequest(
        call_id="call-bp13-with-pack",
        tenant_id="default",
        borrower_id="borrower-bp13-with-pack",
        transcript="kal de dunga",
        turn_meta={"call_date": "2026-06-25"},
        agent_id=PACK_APPEND_MINIMAL.agent_id,
        pack_id=PACK_APPEND_MINIMAL.pack_id,
    )
    with_pack = await handle_turn(
        with_pack_request,
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
        flows=FLOWS,
        overrides=FIXTURE_PROVIDER,
    )

    assert with_pack.reply_text

    audits = await query_turn_audits_by_borrower(memory, "borrower-bp13-with-pack")
    assert audits[0].agent_id == PACK_APPEND_MINIMAL.agent_id
    assert audits[0].pack_id == PACK_APPEND_MINIMAL.pack_id
    assert audits[0].manifest_version == MANIFEST_VERSION

    state = await memory.load_state("call-bp13-with-pack")
    assert state is not None
    assert state.slots["brand_override_pack_id"] == PACK_APPEND_MINIMAL.pack_id


@pytest.mark.asyncio
async def test_handle_turn_without_agent_id_unchanged():
    memory = InMemoryMemoryStore()
    kb, llm, tools = _scripted_ptp_clients()
    await _seed_verified_borrower(memory, "borrower-bp13-no-agent")

    request = TurnRequest(
        call_id="call-bp13-no-agent",
        tenant_id="default",
        borrower_id="borrower-bp13-no-agent",
        transcript="kal de dunga",
        turn_meta={"call_date": "2026-06-25"},
    )
    response = await handle_turn(
        request,
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
        flows=FLOWS,
        overrides=FIXTURE_PROVIDER,
    )

    audits = await query_turn_audits_by_borrower(memory, "borrower-bp13-no-agent")
    assert audits[0].agent_id is None
    assert audits[0].pack_id is None
    assert response.reply_text

    state = await memory.load_state("call-bp13-no-agent")
    assert state is not None
    assert "brand_override_pack_id" not in state.slots
