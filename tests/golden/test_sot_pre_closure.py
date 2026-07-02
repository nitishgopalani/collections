"""Salary On Time pre-closure golden tests — full script, simulated tools.

Exercises the complete pre-closure path through handle_turn with scripted LLM
commands: happy path + close order, objection answer-and-resume, objection
transfer (simulated), third-party C1/C3, and the already-paid loop.
"""

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.retrieval import clear_retrieval_cache
from app.engine.turn import handle_turn
from app.flows.loader import reload_flow_set
from app.schemas.api import TurnRequest

from app.memory.store import InMemoryMemoryStore

CALL_DATE = "2026-06-25"
BORROWER = "sot_test_borrower"


@pytest.fixture(autouse=True)
def _sot_test_mode(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    get_settings.cache_clear()
    reload_flow_set()
    clear_retrieval_cache()
    yield
    clear_retrieval_cache()
    get_settings.cache_clear()


def _req(call_id: str, transcript: str) -> TurnRequest:
    return TurnRequest(
        call_id=call_id,
        tenant_id="salary_on_time",
        borrower_id=BORROWER,
        transcript=transcript,
        turn_meta={"force_flow": "sot_opener", "call_date": CALL_DATE},
    )


async def _run(memory, llm, call_id, transcript):
    return await handle_turn(
        _req(call_id, transcript),
        memory=memory,
        kb=ScriptedKBEmpty(),
        llm=llm,
        tools=FakeToolClient(),
    )


class ScriptedKBEmpty:
    retrieve_calls = 0

    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def retrieve(self, text, tenant_id, k: int = 6):
        return []


def _llm(turns):
    return _ScriptedLLM(turns)


class _ScriptedLLM:
    def __init__(self, turns):
        import json

        self._responses = [json.dumps(t) for t in turns]
        self.call_count = 0

    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def complete(self, system: str, user: str, *, json_only: bool = True) -> str:
        self.call_count += 1
        if self.call_count <= len(self._responses):
            return self._responses[self.call_count - 1]
        return "[]"


@pytest.mark.asyncio
async def test_happy_path_offer_to_close_order():
    memory = InMemoryMemoryStore()
    call_id = "sot-happy"
    llm = _llm(
        [
            [],  # T1 greeting
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [
                {"command": "set_slot", "name": "sot_payment_intent", "value": "willing"},
                {"command": "set_slot", "name": "sot_commit_timing", "value": "today"},
            ],
            [{"command": "set_slot", "name": "sot_customer_time", "value": "shaam 5 baje"}],
            [{"command": "set_slot", "name": "sot_final_confirm", "value": "yes"}],
        ]
    )

    r1 = await _run(memory, llm, call_id, "")
    assert r1.reply_id == "sot_greeting"

    r2 = await _run(memory, llm, call_id, "haan main Rishabh")
    assert r2.reply_id == "sot_offer_pre_closure"

    r3 = await _run(memory, llm, call_id, "haan aaj kar dunga")
    # chained into commitment, asking for time
    assert r3.reply_id in {"sot_ask_time", "sot_confirm_today"}

    r4 = await _run(memory, llm, call_id, "shaam 5 baje")
    assert r4.reply_id == "sot_confirm_today"

    r5 = await _run(memory, llm, call_id, "haan confirm")
    # close: send_whatsapp -> closing utter -> hangup, strict order
    assert r5.reply_id == "sot_close"
    assert r5.end_call is True
    actions = r5.actions_executed
    assert "send_whatsapp_message" in actions
    assert "hangup_call" in actions
    assert actions.index("send_whatsapp_message") < actions.index("hangup_call")

    state = await memory.load_state(call_id)
    assert state.slots.get("payment_link_sent") is True
    assert state.slots.get("whatsapp_simulated") is True


@pytest.mark.asyncio
async def test_objection_penalty_answers_and_resumes_offer():
    memory = InMemoryMemoryStore()
    call_id = "sot-obj-resume"
    llm = _llm(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [{"command": "start_flow", "flow": "sot_obj_penalty"}],  # objection during offer
            [{"command": "set_slot", "name": "sot_payment_intent", "value": "willing"},
             {"command": "set_slot", "name": "sot_commit_timing", "value": "today"}],
        ]
    )

    await _run(memory, llm, call_id, "")
    await _run(memory, llm, call_id, "haan Rishabh")

    r3 = await _run(memory, llm, call_id, "penalty kitni lagegi?")
    assert r3.reply_id == "sot_obj_penalty"
    assert r3.end_call is False
    state = await memory.load_state(call_id)
    # parent offer flow retained on the stack (not dropped, not restarted)
    assert any(f.flow == "sot_offer_pre_closure" for f in state.flow_stack)

    r4 = await _run(memory, llm, call_id, "theek hai aaj kar dunga")
    # resumed at the offer's collect, consumed payment_intent, advanced into commitment
    state2 = await memory.load_state(call_id)
    assert any(f.flow == "sot_commit" for f in state2.flow_stack)
    assert r4.reply_id in {"sot_ask_time", "sot_confirm_today"}


@pytest.mark.asyncio
async def test_objection_never_loan_transfers_simulated():
    memory = InMemoryMemoryStore()
    call_id = "sot-obj-transfer"
    llm = _llm(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [{"command": "start_flow", "flow": "sot_obj_never_loan"}],
        ]
    )
    await _run(memory, llm, call_id, "")
    await _run(memory, llm, call_id, "haan Rishabh")
    r3 = await _run(memory, llm, call_id, "maine to koi loan liya hi nahi")

    assert r3.reply_id == "sot_obj_never_loan"
    assert r3.transfer_to_human is True
    state = await memory.load_state(call_id)
    # Transfer is now requested + bridged via the swappable provider (stub -> pending).
    assert state.slots.get("transfer_requested") is True
    assert state.slots.get("transfer_initiated") is True
    assert state.slots.get("transfer_status") == "pending"
    assert state.slots.get("transfer_to_human") is True


@pytest.mark.asyncio
async def test_objection_pay_later_today_transfers_simulated():
    memory = InMemoryMemoryStore()
    call_id = "sot-obj-pay-later"
    llm = _llm(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [{"command": "start_flow", "flow": "sot_obj_pay_later_today"}],
        ]
    )
    await _run(memory, llm, call_id, "")
    await _run(memory, llm, call_id, "haan Rishabh")
    r3 = await _run(memory, llm, call_id, "aaj thodi der baad kar dunga")

    assert r3.reply_id == "sot_obj_pay_later_today"
    assert r3.transfer_to_human is True
    state = await memory.load_state(call_id)
    assert state.slots.get("transfer_requested") is True
    assert state.slots.get("transfer_initiated") is True
    assert state.slots.get("transfer_status") == "pending"


@pytest.mark.asyncio
async def test_third_party_c1_family_proceeds_third_person():
    memory = InMemoryMemoryStore()
    call_id = "sot-c1"
    llm = _llm(
        [
            [],
            [
                {"command": "set_slot", "name": "sot_identity_response", "value": "relation"},
                {"command": "set_slot", "name": "sot_relation_type", "value": "husband"},
            ],
        ]
    )
    await _run(memory, llm, call_id, "")
    r2 = await _run(memory, llm, call_id, "main inke pati hun")

    assert r2.reply_id == "sot_offer_pre_closure_tp"
    state = await memory.load_state(call_id)
    assert state.slots.get("third_person_mode") is True
    assert state.slots.get("identity_ok") is True


@pytest.mark.asyncio
async def test_third_party_c3_restricted_never_reveals_offer():
    memory = InMemoryMemoryStore()
    call_id = "sot-c3"
    llm = _llm(
        [
            [],
            [
                {"command": "set_slot", "name": "sot_identity_response", "value": "relation"},
                {"command": "set_slot", "name": "sot_relation_type", "value": "cousin"},
            ],
            [{"command": "set_slot", "name": "sot_restricted_followup", "value": "wants_details"}],
        ]
    )
    await _run(memory, llm, call_id, "")
    r2 = await _run(memory, llm, call_id, "main inka cousin hun")

    assert r2.reply_id == "sot_restricted_intro"
    state = await memory.load_state(call_id)
    assert state.slots.get("restricted_mode") is True
    assert state.slots.get("sot_no_detail") is True
    # never chained to the offer flow
    assert not any(f.flow == "sot_offer_pre_closure" for f in state.flow_stack)

    r3 = await _run(memory, llm, call_id, "kitna paisa dena hai?")
    # asks for details -> security deny line, still no offer disclosure
    assert r3.reply_id == "sot_restricted_security"
    assert r3.reply_id not in {"sot_offer_pre_closure", "sot_offer_pre_closure_tp"}


@pytest.mark.asyncio
async def test_already_paid_acknowledges_and_ends():
    """W1.1: 'already paid' is terminal — ack + ask for proof, then close.

    Previously this looped back to re-ask the payment intent, so a borrower who
    had already paid heard the offer again. Now it acknowledges (asks for a
    screenshot/reference), marks CLAIMS_PAID, and hangs up.
    """
    memory = InMemoryMemoryStore()
    call_id = "sot-paid"
    llm = _llm(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [{"command": "set_slot", "name": "sot_payment_intent", "value": "already_paid"}],
        ]
    )
    await _run(memory, llm, call_id, "")
    await _run(memory, llm, call_id, "haan Rishabh")
    r3 = await _run(memory, llm, call_id, "maine to pay kar diya hai")

    assert r3.reply_id == "sot_already_paid"
    assert r3.end_call is True
    assert r3.disposition == "CLAIMS_PAID"
    state = await memory.load_state(call_id)
    assert state.slots.get("sot_payment_intent") is None


@pytest.mark.asyncio
async def test_barge_in_after_close_disconnects():
    """A late barge-in after the call closed must NOT restart the script.

    Once hangup_call has run (end_call + sot_call_closed), any further turn should
    just re-issue end_call with no spoken line so the carrier disconnects, instead of
    idling on a generic clarify with an empty flow stack.
    """
    memory = InMemoryMemoryStore()
    call_id = "sot-bargein-close"
    llm = _llm(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [
                {"command": "set_slot", "name": "sot_payment_intent", "value": "willing"},
                {"command": "set_slot", "name": "sot_commit_timing", "value": "today"},
            ],
            [{"command": "set_slot", "name": "sot_customer_time", "value": "shaam 5 baje"}],
            [{"command": "set_slot", "name": "sot_final_confirm", "value": "yes"}],
            [{"command": "clarify"}],  # T6: late barge-in after close
        ]
    )
    await _run(memory, llm, call_id, "")
    await _run(memory, llm, call_id, "haan main Rishabh")
    await _run(memory, llm, call_id, "haan aaj kar dunga")
    await _run(memory, llm, call_id, "shaam 5 baje")
    r5 = await _run(memory, llm, call_id, "haan confirm")
    assert r5.reply_id == "sot_close"
    assert r5.end_call is True

    # Barge-in after the closing line: terminal guard fires -> no restart, just end.
    r6 = await _run(memory, llm, call_id, "ok bye")
    assert r6.end_call is True
    assert r6.reply_text == ""
    assert r6.reply_id is None


@pytest.mark.asyncio
async def test_cancel_flow_empties_stack_disconnects():
    """When the flow stack empties with nothing left to follow, end the call.

    A borrower who bails mid-offer ('rehne do, band karo') makes the LLM emit
    cancel_flow, which clears the stack. Rather than sit on clarify_general forever,
    the flow-exhaustion guard marks the call closed and disconnects.
    """
    memory = InMemoryMemoryStore()
    call_id = "sot-cancel-end"
    llm = _llm(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [{"command": "cancel_flow"}],
        ]
    )
    await _run(memory, llm, call_id, "")
    await _run(memory, llm, call_id, "haan main Rishabh")
    r3 = await _run(memory, llm, call_id, "rehne do, band karo")

    assert r3.end_call is True
    state = await memory.load_state(call_id)
    assert not state.flow_stack
    assert state.slots.get("sot_call_closed") is True
