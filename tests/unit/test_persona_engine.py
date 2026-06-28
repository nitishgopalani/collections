"""Sprint 10 — Persona Engine tests."""

from datetime import UTC, datetime

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import tenant_config
from app.engine.gate import gate
from app.engine.tracker import hydrate_from_borrower, new_conversation_state
from app.engine.turn import handle_turn
from app.engines_p2.persona import (
    PERSONA_IS_INPUT_NOT_LICENSE,
    build_persona_context,
    classify_persona_llm,
    classify_persona_rules,
    parse_and_validate_persona,
    sync_persona_on_persist,
)
from app.engines_p2.risk import refresh_borrower_risk
from app.engines_p2.trust import refresh_borrower_trust
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.schemas.state import BorrowerRecord
from tests.fixtures.test_borrowers import B_DUE
from tests.fixtures.trust_blueprint_paths import reliable_borrower
from tests.helpers.scripted_clients import ScriptedKB, ScriptedLLM


def _prepare_borrower(borrower: BorrowerRecord) -> BorrowerRecord:
    ref = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    updated = refresh_borrower_trust(borrower)
    return refresh_borrower_risk(updated, reference=ref)


def _reliable_payer() -> BorrowerRecord:
    return _prepare_borrower(reliable_borrower())


def _promise_breaker() -> BorrowerRecord:
    return _prepare_borrower(
        BorrowerRecord(
            borrower_id="B_PROMISE_BREAKER",
            broken_ptps=[
                {"promised_date": "2026-01-10", "broken_on": "2026-01-12"},
                {"promised_date": "2026-01-18", "broken_on": "2026-01-20"},
                {"promised_date": "2026-01-25", "broken_on": "2026-01-28"},
            ],
            ptps=[
                {"promised_date": "2026-01-10", "status": "broken"},
                {"promised_date": "2026-01-18", "status": "broken"},
                {"promised_date": "2026-01-25", "status": "broken"},
            ],
        )
    )


def _salary_dependent() -> BorrowerRecord:
    return _prepare_borrower(
        BorrowerRecord(
            borrower_id="B_SALARY",
            excuses=[
                {"text": "salary delay", "date": "2026-01-05T10:00:00+00:00"},
                {"text": "salary late", "date": "2026-02-05T10:00:00+00:00"},
            ],
            payments=[
                {"date": "2026-01-05T10:00:00+00:00", "full": True, "amount": 5000},
                {"date": "2026-02-05T10:00:00+00:00", "full": True, "amount": 5000},
            ],
        )
    )


def _temporary_hardship() -> BorrowerRecord:
    return _prepare_borrower(
        BorrowerRecord(
            borrower_id="B_HARDSHIP",
            hardships=[{"kind": "medical", "documented": True}],
            excuses=[{"text": "hospital bills", "date": "2026-01-12T10:00:00+00:00"}],
            payments=[
                {"date": "2026-01-15T10:00:00+00:00", "partial": True, "amount": 1500},
                {"date": "2026-01-22T10:00:00+00:00", "partial": True, "amount": 1200},
            ],
            ptps=[{"promised_date": "2026-01-20", "status": "kept", "paid_on": "2026-01-22"}],
        )
    )


def test_reliable_payer_classified_genuine_payer():
    persona = classify_persona_rules(_reliable_payer())
    assert persona.primary_persona == "genuine_payer"
    assert persona.confidence >= 0.6


def test_three_broken_ptps_classified_promise_breaker():
    persona = classify_persona_rules(_promise_breaker())
    assert persona.primary_persona == "promise_breaker"


def test_salary_dependent_payday_pattern():
    persona = classify_persona_rules(_salary_dependent())
    assert persona.primary_persona == "salary_dependent"


def test_hardship_with_partials_not_strategic_defaulter():
    borrower = _temporary_hardship()
    persona = classify_persona_rules(borrower)
    assert persona.primary_persona == "temporary_hardship"
    assert persona.primary_persona != "strategic_defaulter"


def test_primary_secondary_confidence_and_blend():
    persona = classify_persona_rules(_reliable_payer())
    assert persona.primary_persona
    assert persona.confidence > 0
    assert persona.blend
    assert persona.primary_persona in persona.blend


def test_transition_logged_genuine_to_forgetful_to_chronic():
    ref = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    genuine = sync_persona_on_persist(_reliable_payer(), trigger="initial")
    assert genuine.persona_current["primary_persona"] == "genuine_payer"

    forgetful = BorrowerRecord(
        borrower_id="B_FORGET",
        trust_current=68,
        ptps=[
            {"promised_date": "2026-01-10", "status": "kept", "paid_on": "2026-01-10"},
            {"promised_date": "2026-02-05", "status": "broken", "paid_on": None},
        ],
        broken_ptps=[{"promised_date": "2026-02-05", "broken_on": "2026-02-08"}],
        persona_current=genuine.persona_current,
        persona_history=list(genuine.persona_history),
    )
    forgetful = refresh_borrower_risk(forgetful, reference=ref)
    forgetful = sync_persona_on_persist(forgetful, trigger="missed_one_ptp")
    assert forgetful.persona_current["primary_persona"] == "forgetful"
    assert any(
        entry["from"] == "genuine_payer" and entry["to"] == "forgetful"
        for entry in forgetful.persona_history
    )

    chronic = BorrowerRecord(
        borrower_id="B_CHRONIC",
        trust_current=35,
        ptps=[
            {"promised_date": "2026-01-05", "status": "broken"},
            {"promised_date": "2026-01-15", "status": "broken"},
            {"promised_date": "2026-01-25", "status": "broken"},
        ],
        broken_ptps=[
            {"promised_date": "2026-01-05", "broken_on": "2026-01-08"},
            {"promised_date": "2026-01-15", "broken_on": "2026-01-18"},
            {"promised_date": "2026-01-25", "broken_on": "2026-01-28"},
        ],
        notes=[{"text": "kal kar dunga", "kind": "note"}],
        persona_current=forgetful.persona_current,
        persona_history=list(forgetful.persona_history),
    )
    chronic = refresh_borrower_risk(chronic, reference=ref)
    chronic = sync_persona_on_persist(chronic, trigger="repeated_broken_ptp")
    assert chronic.persona_current["primary_persona"] in ("chronic_tomorrow", "promise_breaker")
    assert any(entry["from"] == "forgetful" for entry in chronic.persona_history)


def test_determinism_fixed_inputs():
    borrower = _reliable_payer()
    first = classify_persona_rules(borrower)
    second = classify_persona_rules(borrower)
    assert first.model_dump() == second.model_dump()


def test_persona_is_input_not_license_constant():
    assert PERSONA_IS_INPUT_NOT_LICENSE is True


@pytest.mark.compliance
def test_strategic_defaulter_persona_does_not_relax_gate():
    state = new_conversation_state("c", "default", "b")
    state.slots["persona"] = {
        "primary_persona": "strategic_defaulter",
        "confidence": 0.99,
    }
    cfg = tenant_config("default")
    now = datetime(2026, 6, 25, 10, 0, tzinfo=UTC)
    result = gate("Police aa jayegi agar payment nahi", state, cfg, now=now)
    assert result.verdict == "modify"
    assert result.level == "CRITICAL"
    assert "police" not in result.text.lower()


def test_context_reuses_trust_and_risk_without_recompute():
    borrower = _reliable_payer()
    context = build_persona_context(borrower)
    assert context["trust_current"] == borrower.trust_current
    assert context["risk_flags"] == [
        {"flag": f["flag"], "confidence": f.get("confidence"), "reason": f.get("reason")}
        for f in borrower.risk_flags
    ]
    assert "ability_quadrant" in context
    assert "willingness_quadrant" in context


@pytest.mark.asyncio
async def test_llm_rubric_parses_valid_json():
    borrower = _reliable_payer()
    context = build_persona_context(borrower)
    llm = ScriptedLLM(
        [
            '{"primary_persona":"genuine_payer","secondary_persona":"salary_dependent","confidence":0.82}',
        ]
    )
    persona = await classify_persona_llm(borrower, llm=llm)
    assert persona.primary_persona == "genuine_payer"
    assert persona.secondary_persona == "salary_dependent"
    assert persona.source == "llm_rubric"
    assert llm.call_count == 1
    _ = context


def test_parse_rejects_unknown_persona():
    assert parse_and_validate_persona('{"primary_persona":"hacker","confidence":0.9}') is None


def test_hydrate_exposes_persona_slot():
    borrower = sync_persona_on_persist(_reliable_payer())
    state = new_conversation_state("c", "default", borrower.borrower_id)
    hydrated = hydrate_from_borrower(state, borrower)
    assert hydrated.slots["persona"]["primary_persona"] == "genuine_payer"


@pytest.mark.asyncio
async def test_handle_turn_does_not_add_second_llm_call():
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
    await memory.save_borrower(
        BorrowerRecord(
            borrower_id=B_DUE,
            loan={"amount_due": 5000, "dpd": 45, "bucket": "30-60"},
        )
    )
    for index in range(5):
        await handle_turn(
            TurnRequest(
                call_id=f"persona-lat-{index}",
                tenant_id="default",
                borrower_id=B_DUE,
                transcript="kal payment kar dunga",
                turn_meta={"call_date": "2026-06-25"},
            ),
            memory=memory,
            kb=kb,
            llm=llm,
            tools=FakeToolClient(),
        )
    assert llm.call_count == 5
