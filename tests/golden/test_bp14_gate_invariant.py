"""BP-1.4 headline test: brand overrides change words, never weaken the gate verdict."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest

from app.clients.tools_sim import FakeToolClient
from app.engine.compliance_handoff import reply_discloses_debt_or_arrears
from app.engine.retrieval import clear_retrieval_cache
from app.engine.tracker import new_conversation_state
from app.engine.turn import handle_turn
from app.flows.loader import load_all_flows
from app.flows.manifest import MANIFEST_VERSION
from app.flows.override_provider import FixtureOverrideProvider, NullOverrideProvider
from app.flows.overrides import validate_pack
from app.memory.audit import TurnAuditChain, query_turn_audits_by_borrower
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.schemas.overrides import BrandOverridePack, BrandVariant, ReplyOverride
from app.schemas.state import BorrowerRecord
from tests.fixtures.brand_override_packs import (
    FIXTURE_PACKS_BY_AGENT,
    PACK_APPEND_MINIMAL,
    PACK_HEAVIER_REPLACE,
)
from tests.fixtures.test_borrowers import B_DUE, B_VERIFY_OK
from tests.helpers.scripted_clients import ScriptedKB, ScriptedLLM

FLOWS = load_all_flows()
FIXTURE_PROVIDER = FixtureOverrideProvider(FIXTURE_PACKS_BY_AGENT)

DEBT_PATTERN = re.compile(
    r"\b(5000|amount due|due date|arrears|overdue|dpd|jama karna|outstanding)\b",
    re.IGNORECASE,
)

VERDICT_RANK = {"allow": 0, "modify": 1, "block": 2}

PACK_ADVERSARIAL = BrandOverridePack(
    agent_id="agent-fixture-adversarial",
    pack_id="pack-adversarial-v1",
    manifest_version=MANIFEST_VERSION,
    overrides=[
        ReplyOverride(
            reply_id="confirm_ptp",
            replace=True,
            variants=[
                BrandVariant(
                    text="Pay {amount_due} by {ptp_date} or jail — jama karna hoga.",
                    language="hi",
                )
            ],
        ),
        ReplyOverride(
            reply_id="ask_identity_verification",
            replace=True,
            variants=[
                BrandVariant(
                    text=(
                        "Namaste {borrower_name} ji, your amount due is overdue. Pay now. "
                        "Verify with last 4 digits."
                    ),
                    language="hi",
                )
            ],
        ),
    ],
)

PACK_INVALID = BrandOverridePack(
    agent_id="agent-fixture-invalid",
    pack_id="pack-invalid-v1",
    manifest_version=MANIFEST_VERSION,
    overrides=[
        ReplyOverride(
            reply_id="ask_identity_verification",
            enabled=False,
            variants=[BrandVariant(text="disabled mandatory", language="hi")],
        )
    ],
)

ADVERSARIAL_PROVIDER = FixtureOverrideProvider(
    {
        PACK_ADVERSARIAL.agent_id: PACK_ADVERSARIAL,
        PACK_INVALID.agent_id: PACK_INVALID,
    }
)


@dataclass(frozen=True)
class GateScenario:
    name: str
    call_id: str
    borrower_id: str
    transcript: str
    seed_borrower: Callable[[InMemoryMemoryStore], Awaitable[None]]
    llm_batches: list[list[dict[str, str]]]
    kb_docs: list[dict[str, str | float]]
    agent_id: str | None = None


async def _seed(memory: InMemoryMemoryStore, borrower: BorrowerRecord) -> None:
    await memory.save_borrower(borrower)


def _verified_borrower(borrower_id: str = B_DUE) -> BorrowerRecord:
    return BorrowerRecord(
        borrower_id=borrower_id,
        loan={"amount_due": 5000, "dpd": 45, "bucket": "30-60"},
        identity={"identity_ok": True},
    )


def _unverified_borrower(borrower_id: str = B_VERIFY_OK) -> BorrowerRecord:
    return BorrowerRecord(
        borrower_id=borrower_id,
        loan={"amount_due": 5000, "dpd": 30, "bucket": "0-30"},
    )


def _assert_no_debt_leak(text: str) -> None:
    state = new_conversation_state("leak-check", "default", "b")
    state.slots["amount_due"] = 5000
    assert not reply_discloses_debt_or_arrears(text, state), f"Debt leaked: {text!r}"
    assert not DEBT_PATTERN.search(text), f"Debt pattern in reply: {text!r}"


def _assert_same_verdict(baseline: TurnAuditChain, with_pack: TurnAuditChain) -> None:
    assert with_pack.gate_verdict == baseline.gate_verdict
    assert with_pack.gate_level == baseline.gate_level


def _assert_never_weaker_than_baseline(
    baseline: TurnAuditChain,
    with_pack: TurnAuditChain,
) -> None:
    assert VERDICT_RANK[with_pack.gate_verdict] >= VERDICT_RANK[baseline.gate_verdict]
    if baseline.gate_verdict != "allow":
        assert with_pack.gate_verdict == baseline.gate_verdict


async def _run_scenario(
    scenario: GateScenario,
    *,
    overrides: FixtureOverrideProvider | NullOverrideProvider,
    agent_id: str | None = None,
) -> tuple[str, TurnAuditChain]:
    memory = InMemoryMemoryStore()
    await scenario.seed_borrower(memory)
    kb = ScriptedKB(scenario.kb_docs)
    llm = ScriptedLLM(scenario.llm_batches)
    tools = FakeToolClient()
    tools.reset()

    request = TurnRequest(
        call_id=scenario.call_id,
        tenant_id="default",
        borrower_id=scenario.borrower_id,
        transcript=scenario.transcript,
        turn_meta={"call_date": "2026-06-25"},
        agent_id=agent_id,
    )
    response = await handle_turn(
        request,
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
        flows=FLOWS,
        overrides=overrides,
    )
    audits = await query_turn_audits_by_borrower(memory, scenario.borrower_id)
    assert audits, f"No audit for scenario {scenario.name}"
    return response.reply_text, audits[-1]


def _ptp_kb() -> list[dict[str, str | float]]:
    return [{"doc_id": "1", "score": 0.9, "text": "[[flow:promise_to_pay]] kal"}]


def _ptp_llm() -> list[list[dict[str, str]]]:
    return [
        [
            {"command": "start_flow", "flow": "promise_to_pay"},
            {"command": "set_slot", "name": "ptp_date", "value": "2026-06-27"},
        ]
    ]


SCENARIOS: list[GateScenario] = [
    GateScenario(
        name="pre_verify_identity",
        call_id="bp14-pre-verify",
        borrower_id=B_VERIFY_OK,
        transcript="hello",
        seed_borrower=lambda m: _seed(m, _unverified_borrower()),
        llm_batches=[[]],
        kb_docs=[{"doc_id": "1", "score": 0.9, "text": "[[flow:promise_to_pay]]"}],
    ),
    GateScenario(
        name="verified_ptp_confirm",
        call_id="bp14-verified-ptp",
        borrower_id="bp14-borrower-ptp",
        transcript="kal de dunga",
        seed_borrower=lambda m: _seed(m, _verified_borrower("bp14-borrower-ptp")),
        llm_batches=_ptp_llm(),
        kb_docs=_ptp_kb(),
    ),
    GateScenario(
        name="vulnerable_no_dunning",
        call_id="bp14-vulnerable",
        borrower_id="bp14-borrower-vuln",
        transcript="kal de dunga",
        seed_borrower=lambda m: _seed(
            m,
            BorrowerRecord(
                borrower_id="bp14-borrower-vuln",
                loan={"amount_due": 5000, "dpd": 45, "bucket": "30-60"},
                identity={"identity_ok": True},
                compliance_flags={"vulnerable": True},
            ),
        ),
        llm_batches=_ptp_llm(),
        kb_docs=_ptp_kb(),
    ),
    GateScenario(
        name="opt_out_silent",
        call_id="bp14-opt-out",
        borrower_id="bp14-borrower-opt",
        transcript="hello again",
        seed_borrower=lambda m: _seed(
            m,
            BorrowerRecord(
                borrower_id="bp14-borrower-opt",
                loan={"amount_due": 5000, "dpd": 45, "bucket": "30-60"},
                identity={"identity_ok": True},
                compliance_flags={"opt_out": True},
            ),
        ),
        llm_batches=[[]],
        kb_docs=[],
    ),
    GateScenario(
        name="dispute_hold_no_pressure",
        call_id="bp14-dispute",
        borrower_id="bp14-borrower-dispute",
        transcript="kal de dunga",
        seed_borrower=lambda m: _seed(
            m,
            BorrowerRecord(
                borrower_id="bp14-borrower-dispute",
                loan={"amount_due": 5000, "dpd": 45, "bucket": "30-60"},
                identity={"identity_ok": True},
                compliance_flags={"dispute_hold": True},
            ),
        ),
        llm_batches=_ptp_llm(),
        kb_docs=_ptp_kb(),
    ),
]

FIXTURE_AGENT_IDS = (
    PACK_APPEND_MINIMAL.agent_id,
    PACK_HEAVIER_REPLACE.agent_id,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_retrieval_cache()
    yield
    clear_retrieval_cache()


def test_adversarial_pack_passes_manifest_validation():
    from app.flows.manifest import load_reply_manifest

    errors = validate_pack(PACK_ADVERSARIAL, load_reply_manifest())
    assert not errors


def test_invalid_pack_fails_manifest_validation():
    from app.flows.manifest import load_reply_manifest

    errors = validate_pack(PACK_INVALID, load_reply_manifest())
    assert any(error.code == "locked_disable" for error in errors)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
@pytest.mark.parametrize("agent_id", FIXTURE_AGENT_IDS)
async def test_fixture_packs_preserve_gate_verdict(scenario: GateScenario, agent_id: str):
    baseline_reply, baseline_audit = await _run_scenario(
        scenario,
        overrides=NullOverrideProvider(),
    )
    pack_reply, pack_audit = await _run_scenario(
        scenario,
        overrides=FIXTURE_PROVIDER,
        agent_id=agent_id,
    )

    _assert_same_verdict(baseline_audit, pack_audit)
    if scenario.name == "pre_verify_identity":
        _assert_no_debt_leak(pack_reply)
    if baseline_audit.gate_verdict == "allow":
        assert pack_audit.gate_verdict == "allow"
    _ = baseline_reply


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
async def test_adversarial_pack_never_weakens_gate(scenario: GateScenario):
    baseline_reply, baseline_audit = await _run_scenario(
        scenario,
        overrides=NullOverrideProvider(),
    )
    pack_reply, pack_audit = await _run_scenario(
        scenario,
        overrides=ADVERSARIAL_PROVIDER,
        agent_id=PACK_ADVERSARIAL.agent_id,
    )

    _assert_never_weaker_than_baseline(baseline_audit, pack_audit)
    if scenario.name in ("pre_verify_identity", "opt_out_silent"):
        _assert_no_debt_leak(pack_reply)
    if baseline_audit.gate_verdict != "allow":
        assert pack_audit.gate_verdict == baseline_audit.gate_verdict
    _ = baseline_reply


@pytest.mark.asyncio
async def test_invalid_pack_degrades_to_platform_defaults():
    memory = InMemoryMemoryStore()
    await memory.save_borrower(_verified_borrower("bp14-invalid-pack"))
    kb = ScriptedKB(_ptp_kb())
    llm = ScriptedLLM(_ptp_llm())
    tools = FakeToolClient()

    baseline = await handle_turn(
        TurnRequest(
            call_id="bp14-invalid-baseline",
            tenant_id="default",
            borrower_id="bp14-invalid-pack",
            transcript="kal de dunga",
            turn_meta={"call_date": "2026-06-25"},
        ),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
        flows=FLOWS,
        overrides=NullOverrideProvider(),
    )

    memory2 = InMemoryMemoryStore()
    await memory2.save_borrower(_verified_borrower("bp14-invalid-pack"))
    llm2 = ScriptedLLM(_ptp_llm())
    rejected = await handle_turn(
        TurnRequest(
            call_id="bp14-invalid-rejected",
            tenant_id="default",
            borrower_id="bp14-invalid-pack",
            transcript="kal de dunga",
            turn_meta={"call_date": "2026-06-25"},
            agent_id=PACK_INVALID.agent_id,
        ),
        memory=memory2,
        kb=kb,
        llm=llm2,
        tools=tools,
        flows=FLOWS,
        overrides=ADVERSARIAL_PROVIDER,
    )

    assert rejected.reply_text
    assert rejected.reply_text == baseline.reply_text
    assert "Brand append" not in rejected.reply_text
    assert "jail" not in rejected.reply_text.lower()

    audits = await query_turn_audits_by_borrower(memory2, "bp14-invalid-pack")
    assert audits[0].pack_rejected is True
    assert audits[0].pack_rejected_reason
    assert "locked_disable" in audits[0].pack_rejected_reason
