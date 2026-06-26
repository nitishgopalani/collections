"""BP-1.4: brand overrides merged into live NLG render path."""

import pytest

from app.clients.tools_sim import FakeToolClient
from app.engine.turn import handle_turn
from app.flows.loader import load_all_flows
from app.flows.override_provider import FixtureOverrideProvider, NullOverrideProvider
from app.memory.audit import query_turn_audits_by_borrower
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.schemas.overrides import BrandOverridePack, BrandVariant, ReplyOverride
from app.schemas.state import BorrowerRecord
from tests.fixtures.brand_override_packs import (
    FIXTURE_PACKS_BY_AGENT,
    PACK_APPEND_MINIMAL,
    PACK_HEAVIER_REPLACE,
)
from tests.fixtures.test_borrowers import B_VERIFY_OK
from tests.helpers.scripted_clients import ScriptedKB, ScriptedLLM

FLOWS = load_all_flows()
FIXTURE_PROVIDER = FixtureOverrideProvider(FIXTURE_PACKS_BY_AGENT)

PACK_MANDATORY_WORDING = BrandOverridePack(
    agent_id="agent-mandatory-wording",
    pack_id="pack-mandatory-wording-v1",
    manifest_version="1.0.0",
    overrides=[
        ReplyOverride(
            reply_id="ask_identity_verification",
            replace=True,
            variants=[
                BrandVariant(
                    text=(
                        "[COMPLIANCE-REVIEW] Brand identity line — "
                        "please share your date of birth or last 4 digits."
                    ),
                    language="hi",
                )
            ],
        )
    ],
)
MANDATORY_PROVIDER = FixtureOverrideProvider(
    {PACK_MANDATORY_WORDING.agent_id: PACK_MANDATORY_WORDING}
)


async def _seed_verified_borrower(memory: InMemoryMemoryStore, borrower_id: str) -> None:
    await memory.save_borrower(
        BorrowerRecord(
            borrower_id=borrower_id,
            loan={"amount_due": 5000, "dpd": 45, "bucket": "30-60"},
            identity={"identity_ok": True},
        )
    )


def _ptp_script() -> list[list[dict[str, str]]]:
    return [
        [
            {"command": "start_flow", "flow": "promise_to_pay"},
            {"command": "set_slot", "name": "ptp_date", "value": "2026-06-27"},
        ]
    ]


@pytest.mark.asyncio
async def test_append_pack_renders_brand_variant():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:promise_to_pay]] kal"}])
    llm = ScriptedLLM(_ptp_script())
    tools = FakeToolClient()
    await _seed_verified_borrower(memory, "borrower-bp14-append")

    response = await handle_turn(
        TurnRequest(
            call_id="call-bp14-append",
            tenant_id="default",
            borrower_id="borrower-bp14-append",
            transcript="kal de dunga",
            turn_meta={"call_date": "2026-06-25"},
            agent_id=PACK_APPEND_MINIMAL.agent_id,
        ),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
        flows=FLOWS,
        overrides=FIXTURE_PROVIDER,
    )

    assert "Brand append" in response.reply_text
    assert response.reply_id == "confirm_ptp"


@pytest.mark.asyncio
async def test_append_pack_rotates_through_enlarged_pool():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:promise_to_pay]] kal"}])
    llm = ScriptedLLM(_ptp_script() * 6)
    tools = FakeToolClient()
    await _seed_verified_borrower(memory, "borrower-bp14-rotate")

    replies: set[str] = set()
    for attempt in range(6):
        response = await handle_turn(
            TurnRequest(
                call_id="call-bp14-rotate",
                tenant_id="default",
                borrower_id="borrower-bp14-rotate",
                transcript=f"kal de dunga turn {attempt}",
                turn_meta={"call_date": "2026-06-25"},
                agent_id=PACK_APPEND_MINIMAL.agent_id,
            ),
            memory=memory,
            kb=kb,
            llm=llm,
            tools=tools,
            flows=FLOWS,
            overrides=FIXTURE_PROVIDER,
        )
        replies.add(response.reply_text)

    assert any("Brand append" in text for text in replies)
    assert len(replies) >= 2


@pytest.mark.asyncio
async def test_null_provider_no_regression_vs_platform():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:promise_to_pay]] kal"}])
    llm = ScriptedLLM(_ptp_script())
    tools = FakeToolClient()
    await _seed_verified_borrower(memory, "borrower-bp14-null")

    request = TurnRequest(
        call_id="call-bp14-null",
        tenant_id="default",
        borrower_id="borrower-bp14-null",
        transcript="kal de dunga",
        turn_meta={"call_date": "2026-06-25"},
    )
    baseline = await handle_turn(
        request,
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
        flows=FLOWS,
        overrides=NullOverrideProvider(),
    )

    memory2 = InMemoryMemoryStore()
    await _seed_verified_borrower(memory2, "borrower-bp14-null")
    llm2 = ScriptedLLM(_ptp_script())
    explicit_null = await handle_turn(
        request.model_copy(update={"call_id": "call-bp14-null-2"}),
        memory=memory2,
        kb=kb,
        llm=llm2,
        tools=tools,
        flows=FLOWS,
        overrides=NullOverrideProvider(),
    )

    assert explicit_null.reply_text == baseline.reply_text
    assert "Brand append" not in explicit_null.reply_text
    assert "Heavy replace" not in explicit_null.reply_text


@pytest.mark.asyncio
async def test_mandatory_reply_wording_override_renders_and_passes_gate():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:promise_to_pay]]"}])
    llm = ScriptedLLM([[]])
    tools = FakeToolClient()
    await memory.save_borrower(
        BorrowerRecord(
            borrower_id=B_VERIFY_OK,
            loan={"amount_due": 5000, "dpd": 30, "bucket": "0-30"},
        )
    )

    response = await handle_turn(
        TurnRequest(
            call_id="call-bp14-mandatory",
            tenant_id="default",
            borrower_id=B_VERIFY_OK,
            transcript="hello",
            turn_meta={"call_date": "2026-06-25"},
            agent_id=PACK_MANDATORY_WORDING.agent_id,
        ),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
        flows=FLOWS,
        overrides=MANDATORY_PROVIDER,
    )

    assert "Brand identity line" in response.reply_text
    audits = await query_turn_audits_by_borrower(memory, B_VERIFY_OK)
    assert audits[0].gate_verdict == "allow"


@pytest.mark.asyncio
async def test_heavy_replace_pack_swaps_confirm_ptp_pool():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:promise_to_pay]] kal"}])
    llm = ScriptedLLM(_ptp_script())
    tools = FakeToolClient()
    await _seed_verified_borrower(memory, "borrower-bp14-heavy")

    response = await handle_turn(
        TurnRequest(
            call_id="call-bp14-heavy",
            tenant_id="default",
            borrower_id="borrower-bp14-heavy",
            transcript="kal de dunga",
            turn_meta={"call_date": "2026-06-25"},
            agent_id=PACK_HEAVIER_REPLACE.agent_id,
        ),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
        flows=FLOWS,
        overrides=FIXTURE_PROVIDER,
    )

    assert "Heavy replace" in response.reply_text
    assert "Theek hai sir" not in response.reply_text
