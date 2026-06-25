"""FS-6 follow-up flow tests."""


import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import tenant_config
from app.engine.followup import (
    apply_attempt_tone_register,
    hydrate_followup_from_borrower,
    reply_has_scold_or_threat,
    tone_register_for_attempt,
)
from app.engine.gate import gate
from app.engine.hardship import reply_has_pressure_language
from app.engine.retrieval import clear_retrieval_cache
from app.engine.tracker import new_conversation_state
from app.engine.turn import handle_turn
from app.engines_p2.trust import refresh_borrower_trust
from app.flows.loader import load_all_flows
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.schemas.state import BorrowerRecord
from tests.fixtures.test_borrowers import B_DUE, B_PROCESSING
from tests.helpers.scripted_clients import ScriptedKB, ScriptedLLM

FLOWS = load_all_flows()
_REF_DATE = "2026-06-25"


def _turn(call_id: str, borrower_id: str, transcript: str) -> TurnRequest:
    return TurnRequest(
        call_id=call_id,
        tenant_id="default",
        borrower_id=borrower_id,
        transcript=transcript,
        turn_meta={"call_date": _REF_DATE},
    )


def _verified_borrower(**extra) -> BorrowerRecord:
    base = {
        "borrower_id": B_DUE,
        "loan": {"amount_due": 5000, "dpd": 45, "bucket": "30-60"},
        "identity": {"identity_ok": True},
    }
    base.update(extra)
    return BorrowerRecord(**base)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_retrieval_cache()
    yield
    clear_retrieval_cache()


@pytest.mark.asyncio
async def test_link_nudge_paid_confirms():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:payment_link_nudge]]"}])
    llm = ScriptedLLM([[{"command": "start_flow", "flow": "payment_link_nudge"}]])
    tools = FakeToolClient()
    tools.reset()
    await memory.save_borrower(
        BorrowerRecord(
            borrower_id=B_PROCESSING,
            loan={"amount_due": 5000, "dpd": 10, "bucket": "0-30"},
            identity={"identity_ok": True},
            payment_links=[{"link": "https://pay.sim.example/upi/testlink", "amount": 5000}],
        )
    )

    response = await handle_turn(
        _turn("call-link-paid", B_PROCESSING, "link follow up"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert "verify_payment" in response.actions_executed
    assert response.disposition == "PAYMENT_CONFIRMED"
    assert not reply_has_pressure_language(response.reply_text)


@pytest.mark.asyncio
async def test_link_nudge_unpaid_idempotent_resend():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:payment_link_nudge]]"}])
    llm = ScriptedLLM([[{"command": "start_flow", "flow": "payment_link_nudge"}]])
    tools = FakeToolClient()
    tools.reset()
    existing_link = "https://pay.sim.example/default/abc123"
    await memory.save_borrower(
        _verified_borrower(
            payment_links=[{"link": existing_link, "amount": 5000}],
        )
    )

    response = await handle_turn(
        _turn("call-link-resend", B_DUE, "payment link reminder"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert "prepare_link_resend" in response.actions_executed
    assert "create_payment_link" not in response.actions_executed
    assert tools.write_effect_count("create_payment_link") == 0
    assert existing_link in response.reply_text or "link" in response.reply_text.lower()


@pytest.mark.asyncio
async def test_ptp_kept_positive_close_trust_reward_composes():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:ptp_followup]]"}])
    llm = ScriptedLLM([[{"command": "start_flow", "flow": "ptp_followup"}]])
    tools = FakeToolClient()
    tools.reset()
    await memory.save_borrower(
        BorrowerRecord(
            borrower_id=B_PROCESSING,
            loan={"amount_due": 5000, "dpd": 10, "bucket": "0-30"},
            identity={"identity_ok": True},
            ptps=[{"promised_date": "2026-06-24", "status": "pending"}],
            trust_current=50,
        )
    )

    response = await handle_turn(
        _turn("call-ptp-kept", B_PROCESSING, "ptp follow up"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert "mark_ptp_kept" in response.actions_executed
    assert response.disposition == "PTP_KEPT"
    assert not reply_has_scold_or_threat(response.reply_text)

    borrower = await memory.load_borrower(B_PROCESSING)
    assert any(str(p.get("status")).lower() == "kept" for p in borrower.ptps)
    refreshed = refresh_borrower_trust(borrower)
    assert refreshed.trust_current >= 50


@pytest.mark.asyncio
async def test_ptp_broken_factual_reengage_shorter_window_no_scold():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:ptp_followup]]"}])
    llm = ScriptedLLM([[{"command": "start_flow", "flow": "ptp_followup"}]])
    tools = FakeToolClient()
    tools.reset()
    await memory.save_borrower(
        _verified_borrower(
            trust_current=40,
            ptps=[{"promised_date": "2026-06-20", "status": "pending"}],
        )
    )

    response = await handle_turn(
        _turn("call-ptp-broken", B_DUE, "missed ptp date"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert "mark_ptp_broken" in response.actions_executed
    assert "route_broken_ptp_reengage" in response.actions_executed
    assert not reply_has_scold_or_threat(response.reply_text)
    assert not reply_has_pressure_language(response.reply_text)

    state = await memory.load_state("call-ptp-broken")
    assert any(frame.flow == "promise_to_pay" for frame in state.flow_stack)
    assert state.slots.get("ptp_max_days") == 7


@pytest.mark.asyncio
async def test_callback_request_captured_within_call_window():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:callback_request]]"}])
    llm = ScriptedLLM(
        [
            [
                {"command": "start_flow", "flow": "callback_request"},
                {
                    "command": "set_slot",
                    "name": "callback_window",
                    "value": "2026-06-26T10:00",
                },
            ]
        ]
    )
    tools = FakeToolClient()
    await memory.save_borrower(_verified_borrower())

    response = await handle_turn(
        _turn("call-callback", B_DUE, "baad mein call karo"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert "capture_callback_request" in response.actions_executed
    assert response.disposition == "CALLBACK"
    borrower = await memory.load_borrower(B_DUE)
    assert borrower.comms_prefs.get("callback_requested") is True


@pytest.mark.asyncio
async def test_scheduled_callback_resume_hydrates_context():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([{"doc_id": "1", "score": 0.9, "text": "[[flow:scheduled_callback_resume]]"}])
    llm = ScriptedLLM([[{"command": "start_flow", "flow": "scheduled_callback_resume"}]])
    tools = FakeToolClient()
    await memory.save_borrower(
        _verified_borrower(
            comms_prefs={
                "scheduled_callback": {
                    "window": "2026-06-26T10:00",
                    "context": "Partial payment par baat hui thi.",
                }
            },
            notes=[{"type": "call_context", "text": "Partial payment par baat hui thi."}],
        )
    )

    response = await handle_turn(
        _turn("call-resume", B_DUE, "hello callback"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    assert "resume_scheduled_callback" in response.actions_executed
    assert "Partial payment" in response.reply_text or "baat" in response.reply_text.lower()


def test_escalating_tone_register_by_attempt():
    assert tone_register_for_attempt(1) == "standard"
    assert tone_register_for_attempt(2) == "firm"
    assert tone_register_for_attempt(3) == "serious"
    state = new_conversation_state("c", "default", "b")
    state.attempts = 3
    updated = apply_attempt_tone_register(state)
    assert updated.slots["tone_register"] == "serious"


@pytest.mark.compliance
def test_escalating_attempt_still_gate_blocks_pressure():
    state = new_conversation_state("c", "default", "b")
    state.attempts = 5
    state.slots["identity_ok"] = True
    state.slots["tone_register"] = "serious"
    cfg = tenant_config("default")
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime(2026, 6, 25, 10, 0, tzinfo=ZoneInfo(cfg.call_window_timezone))
    draft = "Please EMI jama karna hoga aaj hi warna police aa jayegi"
    result = gate(draft, state, cfg, now=now)
    assert result.verdict in {"block", "modify"}
    assert "police" not in result.text.lower()
    assert "jama" not in result.text.lower() or result.verdict != "allow"


@pytest.mark.asyncio
async def test_opt_out_preempts_ptp_followup():
    memory = InMemoryMemoryStore()
    kb = ScriptedKB([])
    llm = ScriptedLLM(
        [
            [
                {"command": "start_flow", "flow": "ptp_followup"},
                {"command": "start_flow", "flow": "opt_out"},
            ]
        ]
    )
    tools = FakeToolClient()
    call_id = "call-opt-ptp"
    await memory.save_borrower(
        _verified_borrower(ptps=[{"promised_date": "2026-06-20", "status": "pending"}])
    )

    response = await handle_turn(
        _turn(call_id, B_DUE, "stop calling missed ptp"),
        memory=memory,
        kb=kb,
        llm=llm,
        tools=tools,
    )
    state = await memory.load_state(call_id)
    assert state.slots["compliance_flags"]["opt_out"] is True
    assert response.reply_text


def test_hydrate_followup_loads_ptp_and_link_context():
    borrower = _verified_borrower(
        ptps=[{"promised_date": "2026-06-20", "status": "pending"}],
        payment_links=[{"link": "https://pay.example/x", "amount": 5000}],
        comms_prefs={"scheduled_callback": {"context": "Prior topic"}},
    )
    state = new_conversation_state("c", "default", B_DUE)
    state.slots["call_date"] = _REF_DATE
    hydrated = hydrate_followup_from_borrower(state, borrower)
    assert hydrated.slots.get("open_ptp_date") == "2026-06-20"
    assert hydrated.slots.get("payment_link") == "https://pay.example/x"
    assert hydrated.slots.get("followup_resume") is True


def test_followup_flows_loaded():
    for name in (
        "payment_link_nudge",
        "ptp_followup",
        "callback_request",
        "scheduled_callback_resume",
    ):
        assert name in FLOWS.flows
