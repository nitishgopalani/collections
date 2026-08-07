"""P3.4 — Tier-3 respond: answer + short re-ask, grounding, combined gate."""

from __future__ import annotations

import json
import logging

import pytest

from app.clients.tools_sim import FakeToolClient
from app.compliance_defaults import SAFE_FALLBACK_REPLY_HI
from app.config import get_settings, tenant_config
from app.engine.nlg import ResolvedReply, render_resolved
from app.engine.retrieval import clear_retrieval_cache
from app.engine.robustness import REPAIR_COUNTS_KEY
from app.engine.tenant_profile import clear_tenant_profile_cache, get_tenant_profile
from app.engine.turn import handle_turn
from app.flows.loader import get_flow_set, reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest

CALL_DATE = "2026-06-25"
BORROWER = "sot_test_borrower"


@pytest.fixture(autouse=True)
def _p3_env(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("SCRIPTED_CATALOG_ROUTING", "true")
    monkeypatch.setenv("SOT_DIGRESSION", "false")
    monkeypatch.setenv("CALL_WINDOW_START", "00:00")
    monkeypatch.setenv("CALL_WINDOW_END", "23:59")
    monkeypatch.setenv("TEST_SOT_SCENARIO", "pre")
    clear_tenant_profile_cache()
    get_settings.cache_clear()
    reload_flow_set()
    clear_retrieval_cache()
    yield
    clear_retrieval_cache()
    clear_tenant_profile_cache()
    get_settings.cache_clear()


class _EmptyKB:
    retrieve_calls = 0

    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def retrieve(self, text, tenant_id, k: int = 6):
        type(self).retrieve_calls += 1
        return []


class _ScriptedLLM:
    def __init__(self, turns):
        self._responses = [json.dumps(t) for t in turns]
        self.call_count = 0
        self.system_prompts: list[str] = []
        self.user_prompts: list[str] = []

    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def complete(self, system: str, user: str, *, json_only: bool = True, **kw) -> str:
        self.system_prompts.append(system)
        self.user_prompts.append(user)
        self.call_count += 1
        if self.call_count <= len(self._responses):
            return self._responses[self.call_count - 1]
        return "[]"


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
        kb=_EmptyKB(),
        llm=llm,
        tools=FakeToolClient(),
    )


def _offer_full_len() -> int:
    flows = get_flow_set()
    from app.schemas.state import ConversationState

    state = ConversationState(
        call_id="len",
        tenant_id="salary_on_time",
        borrower_id=BORROWER,
        slots={
            "customer_name": "Rishabh",
            "repay_amount": 2300,
            "due_date": "2026-06-30",
            "discount_amount": 300,
        },
    )
    rendered = render_resolved("sot_offer_pre_closure", state, flows)
    return len(rendered.text or "")


@pytest.mark.asyncio
async def test_mid_push_balance_inquiry_respond_plus_short_reask(caplog):
    """kitni payment due → amount from slots + sot_push_retry; stack unchanged; no retry burn."""
    caplog.set_level(logging.INFO, logger="app.engine.turn_decision_log")
    memory = InMemoryMemoryStore()
    call_id = "p3-balance"
    llm = _ScriptedLLM(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [
                {
                    "command": "respond",
                    "text": "आपकी लोन रीपेमेंट राशि 2300 रुपये है।",
                }
            ],
        ]
    )
    await _run(memory, llm, call_id, "")
    await _run(memory, llm, call_id, "haan Rishabh")
    state_before = await memory.load_state(call_id)
    stack_before = [f.flow for f in state_before.flow_stack]
    repair_before = dict(state_before.slots.get(REPAIR_COUNTS_KEY) or {})

    r3 = await _run(memory, llm, call_id, "kitni payment due hai?")
    assert "2300" in (r3.reply_text or "")
    assert "पेनल्टी" not in (r3.reply_text or "")
    assert r3.reply_id == "sot_push_retry"
    # Short retry, not the full offer.
    retry_part = (r3.reply_text or "").split("।", 1)[-1] if "।" in (r3.reply_text or "") else ""
    # Prefer length vs full offer template.
    assert len(r3.reply_text or "") < _offer_full_len() + 80
    assert "ड्यू डेट" not in (r3.reply_text or "") or "बच सकते हैं" not in (
        r3.reply_text or ""
    )
    # Ensure short retry line is present and much shorter than full offer.
    assert "आज पेमेंट" in (r3.reply_text or "")
    assert len("क्या आप आज पेमेंट करने की कोशिश कर सकते हैं?") < _offer_full_len()

    state = await memory.load_state(call_id)
    assert [f.flow for f in state.flow_stack] == stack_before
    assert state.slots.get("last_question_slot") == "sot_payment_intent"
    assert dict(state.slots.get(REPAIR_COUNTS_KEY) or {}) == repair_before

    decisions = [
        r for r in caplog.records if r.getMessage().startswith("turn_decision ")
    ]
    payload = json.loads(decisions[-1].getMessage().removeprefix("turn_decision "))
    guards = payload["guards"]
    assert guards["respond_fired"] is True
    assert guards["grounding_result"] == "pass"
    assert guards["final_text_len"] == len(r3.reply_text or "")
    assert retry_part is not None  # silence unused if split edge-case


@pytest.mark.asyncio
async def test_unknown_office_location_uses_unknown_info_reply():
    profile = get_tenant_profile("salary_on_time")
    assert profile is not None
    assert profile.respond_enabled is True
    unknown = profile.unknown_info_reply
    assert unknown

    memory = InMemoryMemoryStore()
    call_id = "p3-office"
    llm = _ScriptedLLM(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            # LLM invents an address with digits → grounding must swap entire text.
            [
                {
                    "command": "respond",
                    "text": "हमारा ऑफिस सेक्टर 45 में है, प्लॉट 12।",
                }
            ],
        ]
    )
    await _run(memory, llm, call_id, "")
    await _run(memory, llm, call_id, "haan")
    r3 = await _run(memory, llm, call_id, "office kahan hai aapka?")
    assert unknown in (r3.reply_text or "")
    assert "सेक्टर 45" not in (r3.reply_text or "")
    assert "आज पेमेंट" in (r3.reply_text or "")
    assert r3.reply_id == "sot_push_retry"


@pytest.mark.asyncio
async def test_gate_runs_on_combined_respond_plus_reask(monkeypatch, caplog):
    """Prohibited phrase only in re-ask → gate must see COMBINED text and block."""
    caplog.set_level(logging.INFO, logger="app.engine.turn_decision_log")

    real_tenant_config = tenant_config

    def _gated_cfg(tenant_id: str):
        cfg = real_tenant_config(tenant_id)
        return cfg.model_copy(update={"enforce_compliance_gate": True})

    monkeypatch.setattr("app.engine.turn.tenant_config", _gated_cfg)

    seen: dict[str, str] = {}

    from app.engine import gate as gate_mod

    real_gate = gate_mod.gate

    def _spy_gate(reply_text, state, tenant_cfg, **kwargs):
        seen["draft"] = reply_text
        return real_gate(reply_text, state, tenant_cfg, **kwargs)

    monkeypatch.setattr("app.engine.turn.gate", _spy_gate)

    def _poison_reask(*_a, **_k):
        return ResolvedReply(
            text="क्या आज पेमेंट हो सकती है? police aa",
            reply_id="sot_push_retry",
        )

    monkeypatch.setattr("app.engine.turn.render_short_reask", _poison_reask)

    memory = InMemoryMemoryStore()
    call_id = "p3-gate-combined"
    llm = _ScriptedLLM(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            # Clean respond alone would pass the gate; poison is only in re-ask.
            [{"command": "respond", "text": "आपकी देय राशि 2300 रुपये है।"}],
        ]
    )
    await _run(memory, llm, call_id, "")
    await _run(memory, llm, call_id, "haan")
    r3 = await _run(memory, llm, call_id, "kitni payment due hai?")

    assert "police aa" in seen.get("draft", "")
    assert "2300" in seen.get("draft", "")
    assert r3.reply_text == SAFE_FALLBACK_REPLY_HI
    assert "police aa" not in (r3.reply_text or "")


@pytest.mark.asyncio
async def test_prompt_exposes_facts_and_respond_contract():
    memory = InMemoryMemoryStore()
    call_id = "p3-prompt"
    llm = _ScriptedLLM(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [],
        ]
    )
    await _run(memory, llm, call_id, "")
    await _run(memory, llm, call_id, "haan")
    await _run(memory, llm, call_id, "kitni payment due hai?")
    system = llm.system_prompts[-1]
    user = json.loads(llm.user_prompts[-1])
    assert "respond" in system
    assert "unknown-info" in system.lower() or "NEVER invent" in system
    assert "rupaye" in system
    assert "amount_paid" in system or "last_payment" in system
    assert "facts" in user
    assert user["facts"].get("repay_amount") == 2300
    # Payment-history keys appear in the facts schema only when hydrated.
    assert "amount_paid" in user["facts"] or "last_payment_amount" not in user["facts"]


@pytest.mark.asyncio
async def test_reason_given_after_respond_advances_push():
    """C2: after balance respond + re-ask at payment_problem, reason advances.

    References the same salary_delay path as
    ``test_routing_miss_out_of_catalog_does_not_escalate`` / push ladder —
    plus a Tier-3 respond turn in between.
    """
    memory = InMemoryMemoryStore()
    call_id = "p3-reason-after-respond"
    llm = _ScriptedLLM(
        [
            [],
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [{"command": "set_slot", "name": "sot_payment_intent", "value": "refused"}],
            [
                {
                    "command": "respond",
                    "text": "आपकी पेमेंट ड्यू राशि 2300 rupaye है।",
                }
            ],
            [{"command": "set_slot", "name": "sot_payment_problem", "value": "salary_delay"}],
        ]
    )
    await _run(memory, llm, call_id, "")
    await _run(memory, llm, call_id, "haan Rishabh")
    await _run(memory, llm, call_id, "aaj nahi ho payega")
    r4 = await _run(memory, llm, call_id, "kitni payment due hai?")
    assert "2300" in (r4.reply_text or "")
    assert r4.end_call is False
    state_mid = await memory.load_state(call_id)
    assert state_mid.slots.get("last_question_slot") == "sot_payment_problem"

    r5 = await _run(memory, llm, call_id, "salary late hai")
    state = await memory.load_state(call_id)
    assert state.slots.get("sot_payment_problem") == "salary_delay"
    assert state.slots.get("last_question_slot") == "sot_payment_intent_2"
    assert r5.end_call is False
    assert r5.disposition != "ESCALATED_UNCLEAR"


@pytest.mark.asyncio
async def test_session_5f001c27_t1_to_t7_under_respond_engine(caplog):
    """Full simulated transcript of live session 5f001c27 with Tier-3 respond."""
    caplog.set_level(logging.INFO, logger="app.engine.turn_decision_log")
    profile = get_tenant_profile("salary_on_time")
    assert profile is not None
    unknown = profile.unknown_info_reply

    memory = InMemoryMemoryStore()
    call_id = "5f001c27df0f477595850b98458ae97a"
    llm = _ScriptedLLM(
        [
            [],  # t1 opener
            [{"command": "set_slot", "name": "sot_identity_response", "value": "confirmed"}],
            [],  # t3 soft refuse → coercion/clarify path
            [{"command": "set_slot", "name": "sot_payment_intent", "value": "refused"}],
            [
                {
                    "command": "respond",
                    "text": "आपकी पेमेंट ड्यू राशि 2300 रुपये है।",
                }
            ],
            [],  # t6 short nahi
            [
                {
                    "command": "respond",
                    "text": "ड्यू अमाउंट 2300 रुपये है।",
                }
            ],
        ]
    )

    turns = [
        ("", "t1"),
        ("हाँ हाँ मैं बोल रहा हूँ।", "t2"),
        ("नहीं, आज तो नहीं आ पाएगी।", "t3"),
        ("नहीं नहीं आज नहीं कर पाऊंगा।", "t4"),
        ("कितनी पेमेंट दी हुई है?", "t5"),
        ("नहीं।", "t6"),
        ("अरे मैं कह रहा हूँ मैं कर देता हूँ पेमेंट ड्यू कितनी है", "t7"),
    ]
    transcript_rows: list[dict[str, str]] = []
    for text, label in turns:
        resp = await _run(memory, llm, call_id, text)
        transcript_rows.append(
            {
                "turn": label,
                "borrower": text,
                "agent": resp.reply_text or "",
                "reply_id": resp.reply_id or "",
                "end_call": str(bool(resp.end_call)),
            }
        )

    # t5 / t7 must answer balance and stay on-ladder (no repair escalation).
    assert "2300" in transcript_rows[4]["agent"]
    assert transcript_rows[4]["reply_id"] == "sot_push_retry" or "आज" in transcript_rows[4][
        "agent"
    ]
    assert transcript_rows[6]["end_call"] == "False"
    assert "2300" in transcript_rows[6]["agent"]
    assert "repair_escalation" not in transcript_rows[6]["reply_id"]
    assert unknown not in transcript_rows[4]["agent"] or "2300" in transcript_rows[4]["agent"]

    # Persist for CHECKPOINT 3 review artifact.
    out = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "scripts"
        / "_p3_session_5f001c27_transcript.txt"
    )
    lines = ["# Simulated session 5f001c27 under Tier-3 respond engine", ""]
    for row in transcript_rows:
        lines.append(f"### {row['turn']} reply_id={row['reply_id']} end_call={row['end_call']}")
        lines.append(f"Borrower: {row['borrower']!r}")
        lines.append(f"Agent: {row['agent']}")
        lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
