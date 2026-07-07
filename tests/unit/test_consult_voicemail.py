"""Unit tests for consult voicemail transcript detection and property abort."""

from __future__ import annotations

from typing import Any

import pytest

from app.clients import orchestrator
from app.config import tenant_config
from app.engine import consult_binding, prompt_agent
from app.engine.consult_voicemail import is_voicemail_transcript, reset_phrase_cache
from app.engine.prompt_agent import handle_prompt_turn
from app.ws.session import BrainWSSession


class FakeLLM:
    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = list(replies or ["should not run"])

    async def complete(self, system: str, user: str, **kw: Any) -> str:
        if self.replies:
            return self.replies.pop(0)
        return ""


@pytest.fixture(autouse=True)
def _clean():
    prompt_agent.reset_state()
    reset_phrase_cache()
    yield
    prompt_agent.reset_state()
    reset_phrase_cache()


@pytest.mark.parametrize(
    "text",
    [
        # Strong phrases fire alone.
        "Please record your message.",
        "प्लीज रिकॉर्ड योर मैसेज।",
        "Please leave a message after the beep.",
        "संदेश रिकॉर्ड करें।",
        "Your call has been forwarded to voicemail.",  # "voicemail" strong
    ],
)
def test_is_voicemail_transcript_matches_carrier_phrases(text: str):
    assert is_voicemail_transcript(text)


@pytest.mark.parametrize(
    "text",
    [
        # The exact false-positive transcript from the 2026-07-07 live call.
        "हो है बीन फॉरवर्ड।",
        # Other carrier-connection announcements that are NOT voicemail.
        "Your call has been forwarded.",
        "कॉल है बीइंग फॉरवर्ड।",
        # Weak phrase alone must NOT fire (requires conjunction).
        "the person you are trying to reach is not available",
        "the number you are trying to reach is currently busy",
    ],
)
def test_is_voicemail_transcript_rejects_non_voicemail(text: str):
    assert not is_voicemail_transcript(text)


def test_is_voicemail_transcript_weak_conjunction_fires():
    # Two weak phrases in the same transcript -> fires.
    assert is_voicemail_transcript(
        "the person is not available and the line is currently busy"
    )
    # Weak + strong in the same transcript -> fires.
    assert is_voicemail_transcript(
        "the person is not available, please record your message"
    )


def test_is_voicemail_transcript_rejects_human_greeting():
    assert not is_voicemail_transcript("Haan ji, main property owner bol raha hoon")


async def test_property_voicemail_aborts_without_llm(monkeypatch):
    machine_calls: list[str] = []

    def fake_machine(*, consult_id: str) -> dict[str, Any]:
        machine_calls.append(consult_id)
        return {"status": "retrying"}

    monkeypatch.setattr(orchestrator, "consult_machine_answer", fake_machine)

    consult_uuid = "aaaa1111-bbbb-2222-cccc-333333333333"
    consult_binding.register(
        consult_uuid,
        {
            "tenant_id": "booking-confirm",
            "persona": "persona_property",
            "consult_id": "consult-deadbeef",
            "booking_id": "BK9",
        },
    )
    session = BrainWSSession(
        session_id=consult_uuid.replace("-", ""),
        borrower_id="unknown",
        agent_id="persona_property",
        tenant_id="booking-confirm",
        borrower_context={"booking_id": "BK9"},
        started=True,
    )
    llm = FakeLLM()
    out = await handle_prompt_turn(
        session=session,
        transcript="Your call has been forwarded to voicemail.",
        llm=llm,
        tenant_cfg=tenant_config("booking-confirm"),
    )
    assert out.end_call is True
    assert out.disposition == "VOICEMAIL_DETECTED"
    assert machine_calls == ["consult-deadbeef"]
    assert "BK9" not in prompt_agent.CONSULT_RESULTS
    assert llm.replies == ["should not run"]


async def test_repeat_consult_binding_carries_guest_phone(monkeypatch):
    calls: list[dict[str, Any]] = []

    def fake_start(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "consult_id": "c-2",
            "consult_uuid": "22223333-4444-5555-6666-777788889999",
        }

    monkeypatch.setattr(orchestrator, "consult_start", fake_start)
    session = BrainWSSession(
        session_id="cust-sess",
        borrower_id="caller-1",
        agent_id="persona_customer",
        tenant_id="booking-confirm",
        borrower_context={"borrower_phone": "+919940576170", "booking_id": "BK2"},
        started=True,
    )
    attrs = {"booking_id": "BK2", "hotel": "X", "guest": "Y", "phone": "9810422694"}
    pending = await prompt_agent._start_consult(session, attrs)
    assert pending.consult_id == "c-2"
    bound = consult_binding.lookup("22223333444455556666777788889999")
    assert bound is not None
    assert bound["borrower_phone"] == "+919940576170"
    assert bound["consult_id"] == "c-2"
