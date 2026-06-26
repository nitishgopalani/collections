"""EB-1: per-turn NLG attribution in audit chain and TurnResponse."""

import pytest

from app.clients.tools_sim import FakeToolClient
from app.engine.nlg import render_resolved
from app.engine.turn import handle_turn
from app.flows.loader import load_all_flows
from app.flows.manifest import MANIFEST_VERSION
from app.memory.audit import query_turn_audits_by_borrower
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.schemas.state import BorrowerRecord
from tests.helpers.scripted_clients import ScriptedKB, ScriptedLLM

FLOWS = load_all_flows()


def _ptp_request(
    *,
    call_id: str = "call-eb1",
    borrower_id: str = "borrower-eb1",
    agent_id: str | None = "agent-eb1",
    pack_id: str | None = "pack-eb1",
) -> TurnRequest:
    return TurnRequest(
        call_id=call_id,
        tenant_id="default",
        borrower_id=borrower_id,
        transcript="kal de dunga",
        turn_meta={"call_date": "2026-06-25"},
        agent_id=agent_id,
        pack_id=pack_id,
    )


async def _seed_verified_borrower(memory: InMemoryMemoryStore, borrower_id: str) -> None:
    await memory.save_borrower(
        BorrowerRecord(
            borrower_id=borrower_id,
            loan={"amount_due": 5000, "dpd": 45, "bucket": "30-60"},
            identity={"identity_ok": True},
        )
    )


@pytest.mark.asyncio
async def test_turn_audit_records_render_attribution():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:promise_to_pay]] kal"}])
    llm = ScriptedLLM(
        [
            [
                {"command": "start_flow", "flow": "promise_to_pay"},
                {"command": "set_slot", "name": "ptp_date", "value": "2026-06-27"},
            ]
        ]
    )
    tools = FakeToolClient()
    await _seed_verified_borrower(memory, "borrower-eb1")

    request = _ptp_request()
    response = await handle_turn(
        request,
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )

    state = await memory.load_state("call-eb1")
    assert state is not None
    expected = render_resolved(
        "confirm_ptp",
        state,
        FLOWS,
        locale=request.locale,
        channel=request.channel,
    )

    audits = await query_turn_audits_by_borrower(memory, "borrower-eb1")
    assert len(audits) == 1
    chain = audits[0]

    assert chain.reply_id == expected.reply_id == "confirm_ptp"
    assert chain.variant_index == expected.variant_index
    assert chain.language == expected.language
    assert chain.tone_register == expected.tone_register
    assert response.reply_id == expected.reply_id
    assert response.variant_index == expected.variant_index
    assert response.language == expected.language
    assert response.tone_register == expected.tone_register


@pytest.mark.asyncio
async def test_agent_id_and_pack_id_round_trip():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:promise_to_pay]] kal"}])
    llm = ScriptedLLM(
        [
            [
                {"command": "start_flow", "flow": "promise_to_pay"},
                {"command": "set_slot", "name": "ptp_date", "value": "2026-06-27"},
            ]
        ]
    )
    tools = FakeToolClient()
    await _seed_verified_borrower(memory, "borrower-eb1-meta")

    request = _ptp_request(
        call_id="call-eb1-meta",
        borrower_id="borrower-eb1-meta",
        agent_id="agent-round-trip",
        pack_id="pack-round-trip",
    )
    response = await handle_turn(
        request,
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )

    audits = await query_turn_audits_by_borrower(memory, "borrower-eb1-meta")
    assert audits[0].agent_id == "agent-round-trip"
    assert audits[0].pack_id == "pack-round-trip"
    assert audits[0].manifest_version == MANIFEST_VERSION
    assert response.reply_text


@pytest.mark.asyncio
async def test_turn_without_agent_id_still_works():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:promise_to_pay]] kal"}])
    llm = ScriptedLLM(
        [
            [
                {"command": "start_flow", "flow": "promise_to_pay"},
                {"command": "set_slot", "name": "ptp_date", "value": "2026-06-27"},
            ]
        ]
    )
    tools = FakeToolClient()
    await _seed_verified_borrower(memory, "borrower-eb1-no-agent")

    request = _ptp_request(
        call_id="call-eb1-no-agent",
        borrower_id="borrower-eb1-no-agent",
        agent_id=None,
        pack_id=None,
    )
    response = await handle_turn(
        request,
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )

    audits = await query_turn_audits_by_borrower(memory, "borrower-eb1-no-agent")
    assert audits[0].agent_id is None
    assert audits[0].pack_id is None
    assert audits[0].manifest_version == MANIFEST_VERSION
    assert response.reply_text


@pytest.mark.asyncio
async def test_safety_preempt_writes_attribution_fields_without_error():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([])
    llm = ScriptedLLM([])
    tools = FakeToolClient()

    request = TurnRequest(
        call_id="call-eb1-safety",
        tenant_id="default",
        borrower_id="borrower-eb1-safety",
        transcript="Mere papa hospital mein hain main pay nahi kar sakta",
        turn_meta={"call_date": "2026-06-25"},
        agent_id="agent-safety",
        pack_id="pack-safety",
    )
    response = await handle_turn(
        request,
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )

    audits = await query_turn_audits_by_borrower(memory, "borrower-eb1-safety")
    assert len(audits) == 1
    chain = audits[0]
    assert chain.safety_preempted is True
    assert chain.reply_id is None
    assert chain.variant_index is None
    assert chain.language is None
    assert chain.tone_register is None
    assert chain.agent_id == "agent-safety"
    assert chain.pack_id == "pack-safety"
    assert chain.manifest_version == MANIFEST_VERSION
    assert response.reply_text
    assert response.reply_id is None
    assert response.variant_index is None
