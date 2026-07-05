"""Unit tests for the prompt-mode agent (mocked LLM, mocked orchestrator client)."""

from __future__ import annotations

from typing import Any

import pytest

from app.clients import orchestrator
from app.config import tenant_config
from app.engine import consult_binding, prompt_agent
from app.engine.prompt_agent import handle_prompt_turn
from app.ws.session import BrainWSSession


class FakeLLM:
    """Scripted LLM double recording every (system, user) call."""

    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = list(replies or [])
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        system: str,
        user: str,
        *,
        json_only: bool = True,
        response_schema: Any | None = None,
    ) -> str:
        self.calls.append({"system": system, "user": user, "json_only": json_only})
        if self.replies:
            return self.replies.pop(0)
        return "theek hai."


def make_session(session_id: str = "sess-1", agent_id: str = "persona_customer", **ctx: str):
    return BrainWSSession(
        session_id=session_id,
        borrower_id="caller-1",
        agent_id=agent_id,
        tenant_id="booking-confirm",
        borrower_context=dict(ctx),
        started=True,
    )


@pytest.fixture(autouse=True)
def _clean_prompt_state():
    prompt_agent.reset_state()
    yield
    prompt_agent.reset_state()


@pytest.fixture
def tenant_cfg():
    return tenant_config("booking-confirm")


def test_booking_confirm_tenant_is_prompt_mode(tenant_cfg):
    assert tenant_cfg.agent_mode == "prompt"
    assert tenant_cfg.default_locale == "hi-IN"
    assert tenant_cfg.default_persona == "persona_customer"
    assert set(tenant_cfg.prompt_personas) == {"persona_customer", "persona_property"}
    # The two personas must be genuinely different prompts.
    assert (
        tenant_cfg.prompt_personas["persona_customer"]
        != tenant_cfg.prompt_personas["persona_property"]
    )


async def test_history_grows_and_is_replayed(tenant_cfg):
    llm = FakeLLM(["Booking ID bataiye?", "Hotel ka naam bataiye?"])
    session = make_session()

    out1 = await handle_prompt_turn(
        session=session, transcript="booking confirm karni hai", llm=llm, tenant_cfg=tenant_cfg
    )
    assert out1.reply_text == "Booking ID bataiye?"
    assert prompt_agent.session_history("sess-1") == [
        {"role": "user", "text": "booking confirm karni hai"},
        {"role": "assistant", "text": "Booking ID bataiye?"},
    ]

    out2 = await handle_prompt_turn(
        session=session, transcript="BK123 hai", llm=llm, tenant_cfg=tenant_cfg
    )
    assert out2.reply_text == "Hotel ka naam bataiye?"
    assert len(prompt_agent.session_history("sess-1")) == 4
    # Second LLM call must replay the first exchange.
    assert "USER: booking confirm karni hai" in llm.calls[1]["user"]
    assert "ASSISTANT: Booking ID bataiye?" in llm.calls[1]["user"]
    assert llm.calls[1]["user"].rstrip().endswith("ASSISTANT:")
    assert llm.calls[1]["json_only"] is False


async def test_persona_selected_by_agent_id_and_prompts_differ(tenant_cfg):
    llm = FakeLLM(["a", "b"])
    await handle_prompt_turn(
        session=make_session("s-cust", agent_id="persona_customer"),
        transcript="hello",
        llm=llm,
        tenant_cfg=tenant_cfg,
    )
    await handle_prompt_turn(
        session=make_session("s-prop", agent_id="persona_property"),
        transcript="hello",
        llm=llm,
        tenant_cfg=tenant_cfg,
    )
    assert llm.calls[0]["system"] == tenant_cfg.prompt_personas["persona_customer"]
    assert llm.calls[1]["system"] == tenant_cfg.prompt_personas["persona_property"]
    assert llm.calls[0]["system"] != llm.calls[1]["system"]


async def test_unknown_agent_id_falls_back_to_default_persona(tenant_cfg):
    llm = FakeLLM(["a"])
    await handle_prompt_turn(
        session=make_session("s-x", agent_id="something-else"),
        transcript="hello",
        llm=llm,
        tenant_cfg=tenant_cfg,
    )
    assert llm.calls[0]["system"] == tenant_cfg.prompt_personas["persona_customer"]


async def test_opener_turn_uses_call_connected_placeholder(tenant_cfg):
    llm = FakeLLM(["Namaste, OYO support mein swagat hai."])
    out = await handle_prompt_turn(
        session=make_session(), transcript="", llm=llm, tenant_cfg=tenant_cfg
    )
    assert out.reply_text.startswith("Namaste")
    assert "[CALL CONNECTED" in llm.calls[0]["user"]
    # Empty transcript is not recorded as a user history entry.
    assert prompt_agent.session_history("sess-1") == [
        {"role": "assistant", "text": "Namaste, OYO support mein swagat hai."}
    ]


async def test_booking_context_injected_for_property_leg(tenant_cfg):
    llm = FakeLLM(["Namaste, main Amit bol raha hoon OYO se."])
    session = make_session(
        "s-prop", agent_id="persona_property", booking_id="BK123", guest="Rahul", checkin="10 July"
    )
    await handle_prompt_turn(session=session, transcript="", llm=llm, tenant_cfg=tenant_cfg)
    expected = "BOOKING TO VERIFY: booking_id=BK123, guest=Rahul, checkin=10 July"
    assert expected in llm.calls[0]["user"]


async def test_consult_marker_triggers_orchestrator_and_holds(tenant_cfg, monkeypatch):
    calls: list[dict[str, Any]] = []

    def fake_consult_start(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "consult_id": "c-1",
            "bridge_id": "b-1",
            "consult_channel_id": "chan-9",
            "session_uuid": "sess-1",
            "consult_uuid": "11112222-3333-4444-5555-666677778888",
        }

    monkeypatch.setattr(orchestrator, "consult_start", fake_consult_start)
    llm = FakeLLM(
        [
            "Main property se confirm karke batata hoon, line par bane rahiye. "
            '<consult booking_id=BK123 hotel="Hotel Sunrise" guest=Rahul phone=9990001111>'
        ]
    )
    session = make_session()
    out = await handle_prompt_turn(
        session=session, transcript="BK123, Hotel Sunrise, Rahul", llm=llm, tenant_cfg=tenant_cfg
    )
    # Marker stripped from what TTS speaks; hold text kept.
    assert "<consult" not in out.reply_text
    assert "line par bane rahiye" in out.reply_text
    # The customer is referenced by the brain's OWN session id (the AudioSocket
    # uuid), not an Asterisk channel id.
    assert calls == [
        {
            "session_uuid": "sess-1",
            "consult_destination": "9990001111",
            "caller_id": "",
        }
    ]
    # The property-leg persona binding is registered under the returned
    # consult_uuid (dash-insensitive), carrying the booking context.
    bound = consult_binding.lookup("11112222333344445555666677778888")
    assert bound is not None
    assert bound["persona"] == "persona_property"
    assert bound["tenant_id"] == "booking-confirm"
    assert bound["booking_id"] == "BK123"
    assert bound["hotel"] == "Hotel Sunrise"
    assert bound["guest"] == "Rahul"


async def test_consult_start_failure_speaks_fallback(tenant_cfg, monkeypatch):
    def fail_consult_start(**kwargs: Any) -> dict[str, Any]:
        raise orchestrator.OrchestratorError("480 Temporarily unavailable")

    monkeypatch.setattr(orchestrator, "consult_start", fail_consult_start)
    llm = FakeLLM(["Ek minute. <consult booking_id=BK123 hotel=X guest=Y phone=9990001111>"])
    out = await handle_prompt_turn(
        session=make_session(), transcript="details diye", llm=llm, tenant_cfg=tenant_cfg
    )
    assert "contact nahin kar pa raha" in out.reply_text
    assert "<consult" not in out.reply_text


async def test_property_result_marker_recorded_and_call_ends(tenant_cfg):
    llm = FakeLLM(
        ["Dhanyavaad! <consult_result booking_id=BK123 confirmed=yes note=\"owner confirmed\">"]
    )
    out = await handle_prompt_turn(
        session=make_session("s-prop", agent_id="persona_property"),
        transcript="haan, booking confirm hai",
        llm=llm,
        tenant_cfg=tenant_cfg,
    )
    assert out.end_call is True
    assert out.disposition == "CONSULT_REPORTED"
    assert "<consult_result" not in out.reply_text
    assert prompt_agent.CONSULT_RESULTS["BK123"] == {
        "confirmed": "yes",
        "note": "owner confirmed",
    }


async def test_consult_result_injected_into_customer_turn(tenant_cfg, monkeypatch):
    finish_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        orchestrator,
        "consult_start",
        lambda **kw: {"consult_id": "c-7", "bridge_id": "b", "consult_channel_id": "ch"},
    )
    monkeypatch.setattr(
        orchestrator, "consult_finish", lambda **kw: finish_calls.append(kw) or {}
    )

    llm = FakeLLM(
        [
            "Ruk jaiye. <consult booking_id=BK123 hotel=X guest=Rahul phone=9990001111>",
            "Achhi khabar — aapki booking confirm ho gayi hai!",
        ]
    )
    session = make_session()
    await handle_prompt_turn(
        session=session, transcript="BK123 Hotel X Rahul", llm=llm, tenant_cfg=tenant_cfg
    )
    # Property leg (another session) reports the outcome.
    prompt_agent.CONSULT_RESULTS["BK123"] = {"confirmed": "yes", "note": "owner confirmed"}

    out = await handle_prompt_turn(
        session=session, transcript="kya hua?", llm=llm, tenant_cfg=tenant_cfg
    )
    assert "confirm ho gayi" in out.reply_text
    assert "[CONSULT RESULT: confirmed=yes, note=owner confirmed]" in llm.calls[1]["user"]
    assert finish_calls == [{"consult_id": "c-7", "outcome": "confirmed=yes"}]
    # Result is consumed exactly once.
    assert "BK123" not in prompt_agent.CONSULT_RESULTS


async def test_pending_consult_holds_without_llm_then_fails_over(tenant_cfg, monkeypatch):
    monkeypatch.setattr(
        orchestrator,
        "consult_start",
        lambda **kw: {"consult_id": "c-9", "bridge_id": "b", "consult_channel_id": "ch"},
    )
    finish_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        orchestrator, "consult_finish", lambda **kw: finish_calls.append(kw) or {}
    )
    status = {"status": "ringing"}
    monkeypatch.setattr(orchestrator, "consult_status", lambda **kw: dict(status))

    llm = FakeLLM(
        [
            "Ek minute. <consult booking_id=BK9 hotel=X guest=Y phone=9990001111>",
            "Maaf kijiye, property se sampark nahin ho paya; hum aapko update karenge.",
        ]
    )
    session = make_session("s-hold")
    await handle_prompt_turn(session=session, transcript="details", llm=llm, tenant_cfg=tenant_cfg)

    # No result yet, consult still ringing: canned hold reply, no LLM round-trip.
    out = await handle_prompt_turn(
        session=session, transcript="hello?", llm=llm, tenant_cfg=tenant_cfg
    )
    assert "line par bane rahiye" in out.reply_text
    assert len(llm.calls) == 1

    # Orchestrator now reports the leg failed (telco 480): unknown result injected.
    status["status"] = "failed"
    out = await handle_prompt_turn(
        session=session, transcript="kuch pata chala?", llm=llm, tenant_cfg=tenant_cfg
    )
    assert "sampark nahin ho paya" in out.reply_text
    assert "[CONSULT RESULT: confirmed=unknown" in llm.calls[1]["user"]
    assert finish_calls == [{"consult_id": "c-9", "outcome": "failed"}]


async def test_llm_failure_returns_spoken_fallback(tenant_cfg):
    class BoomLLM:
        async def complete(self, system: str, user: str, **kw: Any) -> str:
            raise RuntimeError("vertex down")

    out = await handle_prompt_turn(
        session=make_session(), transcript="hello", llm=BoomLLM(), tenant_cfg=tenant_cfg
    )
    assert "technical dikkat" in out.reply_text
    assert out.end_call is False


async def test_clear_session_drops_history(tenant_cfg):
    llm = FakeLLM(["a"])
    await handle_prompt_turn(
        session=make_session("s-drop"), transcript="hi", llm=llm, tenant_cfg=tenant_cfg
    )
    assert prompt_agent.session_history("s-drop")
    prompt_agent.clear_session("s-drop")
    assert prompt_agent.session_history("s-drop") == []
