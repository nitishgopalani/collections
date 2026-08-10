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
from app.sim.scripted_clients import ScriptedKB

CALL_DATE = "2026-06-25"
BORROWER = "sot_test_borrower"


@pytest.fixture(autouse=True)
def _sot_test_mode(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("TEST_SOT_SCENARIO", "pre")
    # DEBT-033 (W2-1 fold-in): pin the call window wide-open so these 17
    # fixtures stop flaking outside the default 08:00-19:00 Asia/Kolkata
    # window. Without this, call_window_preempt fires on attempts>=1 when
    # the suite runs in the evening/weekend and asserts a call-window close
    # instead of the scripted reply. Same pin already used by the W1-C tests.
    monkeypatch.setenv("CALL_WINDOW_START", "00:00")
    monkeypatch.setenv("CALL_WINDOW_END", "23:59")
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


async def _run_kb(memory, llm, kb, call_id, transcript):
    return await handle_turn(
        _req(call_id, transcript),
        memory=memory,
        kb=kb,
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
    # Transfer requested; no orchestrator configured in tests -> stub (logged
    # intent only, bot leg ended by the action).
    assert state.slots.get("transfer_requested") is True
    assert state.slots.get("transfer_initiated") is True
    assert state.slots.get("transfer_status") == "stub"
    assert state.slots.get("transfer_to_human") is True


@pytest.mark.asyncio
async def test_objection_pay_later_today_stays_on_ladder(monkeypatch):
    """Catalog mode (intended): pay_later_today is a deflection — excluded while
    awaiting payment_intent. Soft 'aaj thodi der baad kar dunga' is treated as
    willing-today (coercion) → commit/ask_time, not a transfer digression.
    """
    monkeypatch.setenv("SCRIPTED_CATALOG_ROUTING", "true")
    get_settings.cache_clear()
    memory = InMemoryMemoryStore()
    call_id = "sot-obj-pay-later"
    llm = _llm(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            # LLM tries the old transfer digression; catalog rejects it; willing wins.
            [{"command": "start_flow", "flow": "sot_obj_pay_later_today"}],
        ]
    )
    await _run(memory, llm, call_id, "")
    await _run(memory, llm, call_id, "haan Rishabh")
    r3 = await _run(memory, llm, call_id, "aaj thodi der baad kar dunga")

    assert r3.reply_id in {"sot_ask_time", "sot_confirm_today"}
    assert r3.transfer_to_human is False
    state = await memory.load_state(call_id)
    assert any(f.flow == "sot_commit" for f in state.flow_stack)
    assert not any(f.flow == "sot_obj_pay_later_today" for f in state.flow_stack)
    assert state.slots.get("transfer_requested") is not True


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


@pytest.mark.asyncio
async def test_digression_off_skips_retrieval_on_rails(monkeypatch):
    """Catalog (default) and legacy digression-off: on-rails turns never hit the KB."""
    monkeypatch.setenv("SOT_DIGRESSION", "false")
    monkeypatch.setenv("SCRIPTED_CATALOG_ROUTING", "true")
    get_settings.cache_clear()
    memory = InMemoryMemoryStore()
    call_id = "sot-digress-off"
    kb = ScriptedKB(
        [{"doc_id": "1", "score": 0.9, "text": "[[flow:sot_obj_link_request]] link bhejo"}]
    )
    llm = _llm(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [],
        ]
    )
    await _run_kb(memory, llm, kb, call_id, "")
    await _run_kb(memory, llm, kb, call_id, "haan Rishabh")
    calls_before = kb.retrieve_calls
    await _run_kb(memory, llm, kb, call_id, "kaise pay karna hai")
    assert kb.retrieve_calls == calls_before


@pytest.mark.asyncio
async def test_catalog_digression_resumes_parent_after_info_objection(monkeypatch):
    """Tier 2: mid-script info objection from the catalog, then resume the offer.

    No KB retrieval. High-interest is not a deflection, so it stays in the catalog
    while awaiting payment_intent; after the objection resumes, willing advances.
    """
    monkeypatch.setenv("SCRIPTED_CATALOG_ROUTING", "true")
    get_settings.cache_clear()
    memory = InMemoryMemoryStore()
    call_id = "sot-catalog-digress"
    kb = ScriptedKB(
        [{"doc_id": "1", "score": 0.9, "text": "[[flow:sot_obj_high_interest]] byaaj zyada"}]
    )
    llm = _llm(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [{"command": "start_flow", "flow": "sot_obj_high_interest"}],
            [
                {"command": "set_slot", "name": "sot_payment_intent", "value": "willing"},
                {"command": "set_slot", "name": "sot_commit_timing", "value": "today"},
            ],
        ]
    )
    await _run_kb(memory, llm, kb, call_id, "")
    await _run_kb(memory, llm, kb, call_id, "haan Rishabh")

    calls_before = kb.retrieve_calls
    r3 = await _run_kb(memory, llm, kb, call_id, "byaaj itna zyada kyun hai")
    assert kb.retrieve_calls == calls_before  # catalog: never retrieve
    assert r3.reply_id == "sot_obj_high_interest"
    assert r3.end_call is False
    state = await memory.load_state(call_id)
    assert any(f.flow == "sot_offer_pre_closure" for f in state.flow_stack)

    r4 = await _run_kb(memory, llm, kb, call_id, "haan aaj kar dunga")
    state2 = await memory.load_state(call_id)
    assert any(f.flow == "sot_commit" for f in state2.flow_stack)
    assert r4.reply_id in {"sot_ask_time", "sot_confirm_today"}


async def _drive_to_link_request(memory, llm, kb, call_id):
    """Common preamble: greet -> confirm identity -> borrower asks for the link."""
    await _run_kb(memory, llm, kb, call_id, "")
    await _run_kb(memory, llm, kb, call_id, "haan Rishabh")
    r3 = await _run_kb(memory, llm, kb, call_id, "mujhe payment link bhej do")
    # Link sent + we now ask whether it arrived (no longer resumes the push ladder).
    assert r3.reply_id == "sot_obj_link_request"
    assert r3.end_call is False
    assert "send_whatsapp_message" in r3.actions_executed
    state = await memory.load_state(call_id)
    assert state.slots.get("payment_link_sent") is True
    return r3


@pytest.mark.asyncio
async def test_link_request_confirms_receipt_then_hangs_up(monkeypatch):
    """Borrower asks for the link, confirms receipt -> thank + hang up (no loop)."""
    monkeypatch.setenv("SOT_DIGRESSION", "true")
    get_settings.cache_clear()
    memory = InMemoryMemoryStore()
    call_id = "sot-link-received"
    kb = ScriptedKB(
        [{"doc_id": "1", "score": 0.9, "text": "[[flow:sot_obj_link_request]] link"}]
    )
    llm = _llm(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [{"command": "start_flow", "flow": "sot_obj_link_request"}],
            [],  # receipt answer resolved by _coerce_sot_link_received
        ]
    )
    await _drive_to_link_request(memory, llm, kb, call_id)

    r4 = await _run_kb(memory, llm, kb, call_id, "haan mil gaya")
    assert r4.reply_id == "sot_link_thanks_close"
    assert r4.end_call is True
    assert "hangup_call" in r4.actions_executed


@pytest.mark.asyncio
async def test_link_request_not_received_resends_reassures_then_hangs_up(monkeypatch):
    """Borrower says the link didn't arrive -> re-send + reassure + hang up gracefully."""
    monkeypatch.setenv("SOT_DIGRESSION", "true")
    get_settings.cache_clear()
    memory = InMemoryMemoryStore()
    call_id = "sot-link-missing"
    kb = ScriptedKB(
        [{"doc_id": "1", "score": 0.9, "text": "[[flow:sot_obj_link_request]] link"}]
    )
    llm = _llm(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [{"command": "start_flow", "flow": "sot_obj_link_request"}],
            [],  # "nahi mila" resolved by _coerce_sot_link_received
        ]
    )
    await _drive_to_link_request(memory, llm, kb, call_id)

    r4 = await _run_kb(memory, llm, kb, call_id, "abhi tak nahi mila")
    assert r4.reply_id == "sot_link_retry_wait"
    assert r4.end_call is True
    # Silent re-send happened before the reassurance, then we hang up.
    assert r4.actions_executed.count("send_whatsapp_message") >= 1
    assert "hangup_call" in r4.actions_executed


@pytest.mark.asyncio
async def test_link_request_not_received_llm_boolean_still_resends(monkeypatch):
    """Regression: LLM answers the receipt check with boolean-style sot_link_received=false.

    The coercion must be authoritative and normalize the transcript to `not_received`,
    overriding the LLM's `false`, so we still hit the re-send/reassure branch instead of
    the thank-and-close branch (the live-call bug).
    """
    monkeypatch.setenv("SOT_DIGRESSION", "true")
    get_settings.cache_clear()
    memory = InMemoryMemoryStore()
    call_id = "sot-link-missing-bool"
    kb = ScriptedKB(
        [{"doc_id": "1", "score": 0.9, "text": "[[flow:sot_obj_link_request]] link"}]
    )
    llm = _llm(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [{"command": "start_flow", "flow": "sot_obj_link_request"}],
            # LLM mis-answers with a boolean; coercion must override it.
            [{"command": "set_slot", "name": "sot_link_received", "value": "false"}],
        ]
    )
    await _drive_to_link_request(memory, llm, kb, call_id)

    r4 = await _run_kb(memory, llm, kb, call_id, "link to nahi mila hai mere ko abhi")
    assert r4.reply_id == "sot_link_retry_wait"
    assert r4.end_call is True
    assert r4.actions_executed.count("send_whatsapp_message") >= 1
    assert "hangup_call" in r4.actions_executed


class _RecordingLLM(_ScriptedLLM):
    """Scripted LLM that also records the user prompts it received."""

    def __init__(self, turns):
        super().__init__(turns)
        self.user_prompts: list[str] = []

    async def complete(self, system: str, user: str, *, json_only: bool = True) -> str:
        self.user_prompts.append(user)
        return await super().complete(system, user, json_only=json_only)


@pytest.mark.asyncio
async def test_catalog_includes_link_request_without_pinning(monkeypatch):
    """Tier 2: sot_obj_link_request is in the catalog with digression/pinning off."""
    monkeypatch.setenv("SCRIPTED_CATALOG_ROUTING", "true")
    monkeypatch.setenv("SOT_DIGRESSION", "false")
    get_settings.cache_clear()
    memory = InMemoryMemoryStore()
    call_id = "sot-catalog-link"
    kb = ScriptedKB([{"doc_id": "1", "score": 0.5, "text": "[[flow:sot_obj_cash]] cash"}])
    llm = _RecordingLLM(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [],
        ]
    )
    await _run_kb(memory, llm, kb, call_id, "")
    await _run_kb(memory, llm, kb, call_id, "haan Rishabh")
    await _run_kb(memory, llm, kb, call_id, "cash payment")
    assert "sot_obj_link_request" in llm.user_prompts[-1]
    assert "routing_note" in llm.user_prompts[-1]


@pytest.mark.asyncio
async def test_catalog_allows_non_deflection_objection_mid_collect(monkeypatch):
    """Tier 2: non-deflection objections (e.g. cash) stay in the catalog and route."""
    monkeypatch.setenv("SCRIPTED_CATALOG_ROUTING", "true")
    get_settings.cache_clear()
    memory = InMemoryMemoryStore()
    call_id = "sot-catalog-cash"
    kb = ScriptedKB([])
    llm = _llm(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [{"command": "start_flow", "flow": "sot_obj_cash"}],
        ]
    )
    await _run_kb(memory, llm, kb, call_id, "")
    await _run_kb(memory, llm, kb, call_id, "haan Rishabh")
    r3 = await _run_kb(memory, llm, kb, call_id, "cash payment")
    assert r3.reply_id == "sot_obj_cash"
    assert r3.end_call is False


@pytest.mark.asyncio
async def test_catalog_link_request_routes_without_digression_flag(monkeypatch):
    """Tier 2: link_request routes from the catalog with SOT_DIGRESSION off (no pins)."""
    monkeypatch.setenv("SCRIPTED_CATALOG_ROUTING", "true")
    monkeypatch.setenv("SOT_DIGRESSION", "false")
    get_settings.cache_clear()
    memory = InMemoryMemoryStore()
    call_id = "sot-catalog-link-route"
    kb = ScriptedKB([])
    llm = _llm(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [{"command": "start_flow", "flow": "sot_obj_link_request"}],
        ]
    )
    await _run_kb(memory, llm, kb, call_id, "")
    await _run_kb(memory, llm, kb, call_id, "haan Rishabh")
    r3 = await _run_kb(memory, llm, kb, call_id, "link chahiye")
    assert r3.reply_id == "sot_obj_link_request"
    assert r3.end_call is False


@pytest.mark.asyncio
async def test_routing_miss_out_of_catalog_does_not_escalate(monkeypatch):
    """P0.3 under catalog: repeated out-of-catalog start_flow must not escalate.

    Deflection sot_obj_busy is absent while awaiting payment_intent_2; rejected
    jumps are routing misses and must not burn repair retries.
    """
    monkeypatch.setenv("SCRIPTED_CATALOG_ROUTING", "true")
    get_settings.cache_clear()
    memory = InMemoryMemoryStore()
    call_id = "sot-routing-miss-reask"
    kb = ScriptedKB([])
    llm = _llm(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [{"command": "set_slot", "name": "sot_payment_intent", "value": "refused"}],
            [{"command": "set_slot", "name": "sot_payment_problem", "value": "salary_delay"}],
            [{"command": "start_flow", "flow": "sot_obj_busy"}],
            [{"command": "start_flow", "flow": "sot_obj_busy"}],
            [{"command": "start_flow", "flow": "sot_obj_busy"}],
        ]
    )
    await _run_kb(memory, llm, kb, call_id, "")
    await _run_kb(memory, llm, kb, call_id, "haan Rishabh")
    await _run_kb(memory, llm, kb, call_id, "aaj nahi ho payega")
    await _run_kb(memory, llm, kb, call_id, "salary late hai")
    state = await memory.load_state(call_id)
    assert state.slots.get("last_question_slot") == "sot_payment_intent_2"

    for text in ("busy hun", "baad mein call karo", "meeting mein hun"):
        resp = await _run_kb(memory, llm, kb, call_id, text)
        assert resp.disposition != "ESCALATED_UNCLEAR"
        assert resp.end_call is False
        assert resp.reply_id != "sot_obj_busy"

    state2 = await memory.load_state(call_id)
    assert state2.slots.get("disposition") != "ESCALATED_UNCLEAR"
    assert state2.slots.get("last_question_slot") == "sot_payment_intent_2"
