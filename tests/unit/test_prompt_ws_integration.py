"""WS integration tests for prompt mode (EB-6 contract, client_id=booking-confirm).

Same style as test_eb6_ws_contract.py: a real /ws/brain connection through the
FastAPI app, but the tenant resolves (via client_id) to the prompt-mode
booking-confirm tenant, so turns are answered by prompt_agent instead of the
flow engine. The LLM is a scripted fake installed on app.state; the
ari-orchestrator client is monkeypatched — ZERO telephony.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.testclient import TestClient

from app.clients import orchestrator
from app.engine import prompt_agent
from app.main import app


class ScriptedLLM:
    """Returns queued replies; records every (system, user) LLM call."""

    is_stub = True

    def __init__(self) -> None:
        self.replies: list[str] = []
        self.calls: list[dict[str, str]] = []

    async def complete(
        self,
        system: str,
        user: str,
        *,
        json_only: bool = True,
        response_schema: Any | None = None,
    ) -> str:
        self.calls.append({"system": system, "user": user})
        if self.replies:
            return self.replies.pop(0)
        return "theek hai."

    async def ping(self) -> bool:  # pragma: no cover - health endpoint only
        return True


@pytest.fixture(autouse=True)
def _clean_prompt_state():
    prompt_agent.reset_state()
    yield
    prompt_agent.reset_state()


def _drive_turn(ws, session_id: str, turn_id: str, transcript: str) -> dict[str, Any]:
    """Send one turn and collect chunks/flow_class until done; return summary."""
    ws.send_json(
        {
            "type": "turn",
            "session_id": session_id,
            "turn_id": turn_id,
            "transcript": transcript,
            "flow_class": "Default",
        }
    )
    chunks: list[str] = []
    seqs: list[int] = []
    types: list[str] = []
    done: dict[str, Any] = {}
    for _ in range(20):
        msg = json.loads(ws.receive_text())
        types.append(msg["type"])
        if msg["type"] == "chunk":
            assert msg["turn_id"] == turn_id
            chunks.append(msg["text"])
            seqs.append(msg["seq"])
        if msg["type"] == "flow_class":
            assert msg["next"] in {"YesNo", "Default", "SpelledInput"}
        if msg["type"] == "done":
            assert msg["turn_id"] == turn_id
            done = msg
            break
        if msg["type"] == "error":
            pytest.fail(msg.get("fallback_text", "brain ws error"))
    else:
        pytest.fail(f"no done message, got: {types}")
    assert seqs == list(range(len(seqs)))
    assert "flow_class" in types and types[-1] == "done"
    return {"chunks": chunks, "reply": " ".join(chunks), "done": done, "types": types}


def _start_session(ws, session_id: str, agent_id: str, borrower_context: dict | None = None):
    ws.send_json(
        {
            "type": "session_start",
            "session_id": session_id,
            "borrower_id": "caller-1",
            "agent_id": agent_id,
            "client_id": "booking-confirm",
            "borrower_context": borrower_context or {},
        }
    )
    ready = json.loads(ws.receive_text())
    assert ready["type"] == "session_ready"
    assert ready["session_id"] == session_id
    return ready


def test_booking_confirm_session_gets_prompt_mode_replies():
    llm = ScriptedLLM()
    llm.replies = [
        "Namaste! OYO support. Apna booking ID bataiye?",
        "Shukriya. Hotel ka naam kya hai?",
    ]
    with TestClient(app) as client:
        app.state.llm = llm
        with client.websocket_connect("/ws/brain") as ws:
            ready = _start_session(ws, "sess-bc-1", "persona_customer")
            # Tenant default locale hi-IN drives the ASR language.
            assert ready["asr_language"] == "hi-IN"

            out1 = _drive_turn(ws, "sess-bc-1", "t-1", "")
            assert out1["reply"].startswith("Namaste")
            assert out1["done"]["end_call"] is False

            out2 = _drive_turn(ws, "sess-bc-1", "t-2", "booking confirm karni hai BK123")
            assert "Hotel ka naam" in out2["reply"]

            ws.send_json({"type": "session_end", "session_id": "sess-bc-1"})

    # Prompt mode was used: the LLM saw the persona prompt, and history replayed.
    assert len(llm.calls) == 2
    assert "OYO customer-support voice agent" in llm.calls[0]["system"]
    assert "USER: booking confirm karni hai BK123" in llm.calls[1]["user"]
    # Session cleanup dropped the in-memory history.
    assert prompt_agent.session_history("sess-bc-1") == []


def test_two_session_consult_round_trip(monkeypatch):
    """Part 4 scripted QA: customer triggers consult -> property answers ->
    customer relays the result. Orchestrator client fully mocked."""
    started: list[dict[str, Any]] = []
    finished: list[dict[str, Any]] = []
    monkeypatch.setattr(
        orchestrator,
        "consult_start",
        lambda **kw: started.append(kw)
        or {"consult_id": "c-100", "bridge_id": "b-1", "consult_channel_id": "consult-chan-1"},
    )
    monkeypatch.setattr(orchestrator, "consult_finish", lambda **kw: finished.append(kw) or {})

    llm = ScriptedLLM()
    llm.replies = [
        # customer turn 1: collect details
        "Booking ID, hotel aur guest ka naam bataiye?",
        # customer turn 2: trigger consult
        "Main property se confirm karke batata hoon, line par bane rahiye. "
        '<consult booking_id=BK123 hotel="Hotel Sunrise" guest=Rahul phone=9990001111>',
        # property opener
        "Namaste, main Amit bol raha hoon OYO se. Booking BK123, guest Rahul, "
        "10 July check-in — kya aap is booking ko confirm karte hain?",
        # property closes with the structured result
        "Bahut shukriya, aapka din shubh ho. "
        '<consult_result booking_id=BK123 confirmed=yes note="owner confirmed">',
        # customer relays
        "Achhi khabar! Property ne aapki booking BK123 confirm kar di hai.",
    ]

    with TestClient(app) as client:
        app.state.llm = llm
        with client.websocket_connect("/ws/brain") as cust, client.websocket_connect(
            "/ws/brain"
        ) as prop:
            _start_session(cust, "sess-cust", "persona_customer", {"channel_id": "ast-chan-1"})
            _start_session(
                prop,
                "sess-prop",
                "persona_property",
                {"booking_id": "BK123", "guest": "Rahul", "checkin": "10 July"},
            )

            # Customer: ask + provide details -> consult marker fires.
            _drive_turn(cust, "sess-cust", "t-1", "meri booking confirm karni hai")
            out = _drive_turn(
                cust, "sess-cust", "t-2", "BK123, Hotel Sunrise, guest Rahul"
            )
            assert "<consult" not in out["reply"]
            assert started == [
                {
                    "customer_channel_id": "ast-chan-1",
                    "consult_destination": "9990001111",
                    "caller_id": "",
                }
            ]

            # Property leg: opener + owner confirms -> result recorded, call ends.
            _drive_turn(prop, "sess-prop", "t-1", "")
            out = _drive_turn(prop, "sess-prop", "t-2", "haan bhai, booking confirm hai")
            assert out["done"]["end_call"] is True
            assert "<consult_result" not in out["reply"]
            assert prompt_agent.CONSULT_RESULTS["BK123"]["confirmed"] == "yes"

            # Customer: next turn relays the injected consult result.
            out = _drive_turn(cust, "sess-cust", "t-3", "kuch pata chala?")
            assert "confirm kar di" in out["reply"]
            assert finished == [{"consult_id": "c-100", "outcome": "confirmed=yes"}]

            cust.send_json({"type": "session_end", "session_id": "sess-cust"})
            prop.send_json({"type": "session_end", "session_id": "sess-prop"})

    # Both persona prompts were actually used and are different.
    customer_systems = {c["system"] for c in llm.calls[:2]} | {llm.calls[4]["system"]}
    property_systems = {llm.calls[2]["system"], llm.calls[3]["system"]}
    assert len(customer_systems) == 1
    assert len(property_systems) == 1
    assert customer_systems != property_systems
    assert "Amit calling from OYO" in next(iter(property_systems))
    # The relay turn saw the injected system message.
    assert "[CONSULT RESULT: confirmed=yes, note=owner confirmed]" in llm.calls[4]["user"]
    # Property leg saw its booking context.
    assert "BOOKING TO VERIFY: booking_id=BK123" in llm.calls[2]["user"]
