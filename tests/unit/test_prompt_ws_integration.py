"""WS integration tests for prompt mode (EB-6 contract, client_id=booking-confirm).

Same style as test_eb6_ws_contract.py: a real /ws/brain connection through the
FastAPI app, but the tenant resolves (via client_id) to the prompt-mode
booking-confirm tenant, so turns are answered by prompt_agent instead of the
flow engine. The LLM is a scripted fake installed on app.state; the
ari-orchestrator client is monkeypatched — ZERO telephony.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import anyio
import pytest
from starlette.testclient import TestClient

from tests.contract_helpers import expected_consult_start
from app.clients import orchestrator
from app.config import get_settings
from app.engine import consult_binding, prompt_agent
from app.main import app
from app.ws import handler as ws_handler


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


def _send_playback_done(ws, session_id: str, turn_id: str) -> None:
    """Simulate the go-server reporting a turn's audio finished playing."""
    ws.send_json(
        {"type": "playback_done", "session_id": session_id, "turn_id": turn_id}
    )


def _wait_for(predicate, timeout_s: float = 5.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail(f"timed out waiting for {what}")


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


def _start_session_with_tenant_id(ws, session_id: str, tenant_id: str, agent_id: str = "default"):
    ws.send_json(
        {
            "type": "session_start",
            "session_id": session_id,
            "borrower_id": "caller-1",
            "agent_id": agent_id,
            "tenant_id": tenant_id,
            "borrower_context": {},
        }
    )
    ready = json.loads(ws.receive_text())
    assert ready["type"] == "session_ready"
    assert ready["session_id"] == session_id
    return ready


def test_test_mode_tenant_id_still_reaches_conference_tenant(monkeypatch):
    """1725617003 (conference) reaches the brain as tenant_id=conference from the
    go-server; TEST_MODE must not pin it to salary_on_time / sot_opener."""
    monkeypatch.setenv("TEST_MODE", "true")
    get_settings.cache_clear()
    llm = ScriptedLLM()
    llm.replies = ["नमस्ते, conference line connected. आप बोल सकते हैं."]
    try:
        with TestClient(app) as client:
            app.state.llm = llm
            with client.websocket_connect("/ws/brain") as ws:
                _start_session_with_tenant_id(ws, "sess-conf-1", "conference")
                out = _drive_turn(ws, "sess-conf-1", "t-1", "")
                assert "conference line connected" in out["reply"]
                ws.send_json({"type": "session_end", "session_id": "sess-conf-1"})
        assert len(llm.calls) == 1
        assert "conference call moderator" in llm.calls[0]["system"]
    finally:
        get_settings.cache_clear()


def test_test_mode_client_id_still_reaches_prompt_tenant(monkeypatch):
    """The live server runs TEST_MODE (pinned to salary_on_time). An explicit
    client_id naming a prompt-mode tenant must still route to prompt mode so the
    booking-confirm DID works there without flipping TEST_MODE off."""
    monkeypatch.setenv("TEST_MODE", "true")
    get_settings.cache_clear()
    llm = ScriptedLLM()
    llm.replies = ["Namaste! OYO support. Booking ID bataiye?"]
    try:
        with TestClient(app) as client:
            app.state.llm = llm
            with client.websocket_connect("/ws/brain") as ws:
                _start_session(ws, "sess-tm-1", "persona_customer")
                out = _drive_turn(ws, "sess-tm-1", "t-1", "")
                assert out["reply"].startswith("Namaste")
                ws.send_json({"type": "session_end", "session_id": "sess-tm-1"})
        # Prompt mode (not the SOT flow engine) answered: the persona prompt ran.
        assert len(llm.calls) == 1
        assert "OYO customer-support voice agent" in llm.calls[0]["system"]
    finally:
        get_settings.cache_clear()


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
    # No consult_uuid in the mocked response -> no binding; the property leg is
    # driven with an explicit persona_property agent_id (pre-Stasis behaviour,
    # still supported).

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

            # Customer: ask + provide details -> consult marker fires. The
            # dial is deferred until the hold announcement finishes playing.
            _drive_turn(cust, "sess-cust", "t-1", "meri booking confirm karni hai")
            out = _drive_turn(
                cust, "sess-cust", "t-2", "BK123, Hotel Sunrise, guest Rahul"
            )
            assert "<consult" not in out["reply"]
            assert started == []
            _send_playback_done(cust, "sess-cust", "t-2")
            _wait_for(lambda: bool(started), what="deferred consult start")
            assert started == [expected_consult_start("sess-cust", "9990001111")]

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
    assert "Amit from OYO" in next(iter(property_systems))
    # The relay turn saw the injected system message.
    assert "[CONSULT RESULT: confirmed=yes, note=owner confirmed]" in llm.calls[4]["user"]
    # Property leg saw its booking context.
    assert "BOOKING TO VERIFY: booking_id=BK123" in llm.calls[2]["user"]


def test_property_session_binds_by_consult_uuid(monkeypatch):
    """Stasis-inbound wiring: consult_start returns the consult AI leg's uuid;
    the property session then arrives with THAT uuid (dash-less, as the
    connector forwards it) as its session_id and a connector-default agent_id.
    The binding must flip it to persona_property with the booking context
    injected — and be unregistered when the property session ends."""
    consult_uuid = "aaaabbbb-cccc-4ddd-8eee-ffff00001111"
    prop_session_id = consult_uuid.replace("-", "")

    started: list[dict[str, Any]] = []
    finished: list[dict[str, Any]] = []
    monkeypatch.setattr(
        orchestrator,
        "consult_start",
        lambda **kw: started.append(kw)
        or {
            "consult_id": "c-500",
            "bridge_id": "b-5",
            "consult_channel_id": "consult-c-500-leg",
            "session_uuid": "sess-cust2",
            "consult_uuid": consult_uuid,
        },
    )
    monkeypatch.setattr(orchestrator, "consult_finish", lambda **kw: finished.append(kw) or {})

    llm = ScriptedLLM()
    llm.replies = [
        # customer turn: trigger consult
        "Main property se confirm karke batata hoon, line par bane rahiye. "
        '<consult booking_id=BK777 hotel="Hotel Moonrise" guest=Sita phone=9990001111>',
        # property opener (must run under persona_property via the binding)
        "Namaste, main Amit bol raha hoon OYO se. Booking BK777, guest Sita — "
        "kya aap is booking ko confirm karte hain?",
        # property closes with the structured result
        "Bahut shukriya. "
        '<consult_result booking_id=BK777 confirmed=yes note="owner confirmed">',
        # customer relays
        "Achhi khabar! Property ne aapki booking BK777 confirm kar di hai.",
    ]

    with TestClient(app) as client:
        app.state.llm = llm
        with client.websocket_connect("/ws/brain") as cust, client.websocket_connect(
            "/ws/brain"
        ) as prop:
            _start_session(cust, "sess-cust2", "persona_customer")
            out = _drive_turn(cust, "sess-cust2", "t-1", "BK777, Hotel Moonrise, guest Sita")
            assert "<consult" not in out["reply"]
            _send_playback_done(cust, "sess-cust2", "t-1")
            _wait_for(lambda: bool(started), what="deferred consult start")
            assert started == [expected_consult_start("sess-cust2", "9990001111")]

            # Property leg: session_id IS the (dash-less) consult uuid; the
            # connector knows nothing about personas and sends its default
            # agent_id — the binding must override it.
            _start_session(prop, prop_session_id, "connector-default")
            _drive_turn(prop, prop_session_id, "t-1", "")
            out = _drive_turn(prop, prop_session_id, "t-2", "haan, booking confirm hai")
            assert out["done"]["end_call"] is True
            assert prompt_agent.CONSULT_RESULTS["BK777"]["confirmed"] == "yes"

            # Customer relays the outcome.
            out = _drive_turn(cust, "sess-cust2", "t-2", "kuch pata chala?")
            assert "confirm kar di" in out["reply"]
            assert finished == [{"consult_id": "c-500", "outcome": "confirmed=yes"}]

            prop.send_json({"type": "session_end", "session_id": prop_session_id})
            cust.send_json({"type": "session_end", "session_id": "sess-cust2"})

    # The property turns ran under persona_property with the booking context —
    # NOT under the connector-default/customer persona.
    property_calls = [c for c in llm.calls if "Amit from OYO" in c["system"]]
    assert len(property_calls) == 2
    assert "BOOKING TO VERIFY: booking_id=BK777, hotel=Hotel Moonrise, guest=Sita" in (
        property_calls[0]["user"]
    )
    # The binding was unregistered with the property session's end.
    assert consult_binding.lookup(prop_session_id) is None


def _start_consult_via_turns(ws, llm: ScriptedLLM, session_id: str) -> None:
    """Drive the customer to the point where a consult is pending."""
    llm.replies.insert(
        0,
        "Main property se confirm karke batata hoon, line par bane rahiye. "
        '<consult booking_id=BK123 hotel="Hotel Sunrise" guest=Rahul phone=9990001111>',
    )
    out = _drive_turn(ws, session_id, "t-consult", "BK123, Hotel Sunrise, guest Rahul")
    assert "<consult" not in out["reply"]
    # The dial waits for the announcement's playback_done.
    assert not prompt_agent.has_pending_consult(session_id)
    _send_playback_done(ws, session_id, "t-consult")
    _wait_for(
        lambda: prompt_agent.has_pending_consult(session_id),
        what="deferred consult start",
    )


def _receive_json_timeout(ws, timeout_s: float) -> dict[str, Any] | None:
    """Bounded receive: one frame as dict, or None if nothing arrives in time.

    starlette's WebSocketTestSession.receive_text() blocks forever when the app
    stays silent — correct-silence scenarios (e.g. the watcher intentionally
    not pushing while retries remain) must read with a per-frame timeout so
    "no frame" is a clean outcome instead of a suite hang.
    """

    async def _recv_bounded():
        with anyio.move_on_after(timeout_s):
            return await ws._send_rx.receive()
        return None

    message = ws.portal.call(_recv_bounded)
    if message is None:
        return None
    ws._raise_on_close(message)
    return json.loads(message["text"])


def _collect_push(ws, timeout_s: float = 5.0) -> dict[str, Any]:
    """Collect one unsolicited chunk/flow_class/done unit (no turn was sent)."""
    chunks: list[str] = []
    done: dict[str, Any] = {}
    turn_ids: set[str] = set()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        msg = _receive_json_timeout(ws, timeout_s=max(0.05, deadline - time.monotonic()))
        if msg is None:
            break
        if msg["type"] == "chunk":
            chunks.append(msg["text"])
            turn_ids.add(msg["turn_id"])
        if msg["type"] == "done":
            done = msg
            turn_ids.add(msg["turn_id"])
            break
    assert done, "no unsolicited done frame arrived within the poll budget"
    assert len(turn_ids) == 1, f"push frames span multiple turn_ids: {turn_ids}"
    return {"chunks": chunks, "reply": " ".join(chunks), "done": done}


def test_silent_customer_still_hears_consult_result(monkeypatch):
    """Reviewer gap: during hold the customer is silent (MOH), so no turns
    arrive. The watcher must push the confirmed result as an unsolicited turn
    within the poll budget."""
    monkeypatch.setattr(ws_handler, "CONSULT_PUSH_POLL_S", 0.05)
    # Patch the HANDLER's imported binding: handler.py does
    # `from app.engine.prompt_agent import derive_consult_push_budget_s`,
    # so patching prompt_agent's symbol never reaches the watcher.
    monkeypatch.setattr(ws_handler, "derive_consult_push_budget_s", lambda **_kw: 5.0)
    finished: list[dict[str, Any]] = []
    monkeypatch.setattr(
        orchestrator,
        "consult_start",
        lambda **kw: {"consult_id": "c-200", "bridge_id": "b-1", "consult_channel_id": "cc-1"},
    )
    monkeypatch.setattr(orchestrator, "consult_finish", lambda **kw: finished.append(kw) or {})

    llm = ScriptedLLM()
    llm.replies = [
        # relay reply produced by the watcher's build_consult_relay call
        "Achhi khabar! Property ne aapki booking BK123 confirm kar di hai.",
    ]

    with TestClient(app) as client:
        app.state.llm = llm
        with client.websocket_connect("/ws/brain") as ws:
            _start_session(ws, "sess-silent", "persona_customer", {"channel_id": "ast-1"})
            _start_consult_via_turns(ws, llm, "sess-silent")

            # Property leg posts the outcome; the CUSTOMER SENDS NO MORE TURNS.
            prompt_agent.CONSULT_RESULTS["BK123"] = {
                "confirmed": "yes",
                "note": "owner confirmed",
            }

            push = _collect_push(ws)
            assert push["done"]["turn_id"].startswith("consult-push-")
            assert push["done"]["disposition"] == "CONSULT_RELAYED"
            assert push["done"]["end_call"] is False
            assert "confirm kar di" in push["reply"]

            ws.send_json({"type": "session_end", "session_id": "sess-silent"})

    # The consult was closed out with the outcome.
    assert finished == [{"consult_id": "c-200", "outcome": "confirmed=yes"}]
    # The relay LLM call saw the injected result.
    assert "[CONSULT RESULT: confirmed=yes, note=owner confirmed]" in llm.calls[-1]["user"]


def test_silent_customer_gets_failure_push_when_budget_expires(monkeypatch):
    """No result ever arrives: after the budget the watcher pushes the
    could-not-reach fallback instead of leaving the caller on hold forever."""
    monkeypatch.setattr(ws_handler, "CONSULT_PUSH_POLL_S", 0.05)
    # Patch the handler's imported binding (see note in the 5.0s tests).
    monkeypatch.setattr(ws_handler, "derive_consult_push_budget_s", lambda **_kw: 0.2)
    finished: list[dict[str, Any]] = []
    monkeypatch.setattr(
        orchestrator,
        "consult_start",
        lambda **kw: {"consult_id": "c-201", "bridge_id": "b-1", "consult_channel_id": "cc-1"},
    )
    monkeypatch.setattr(orchestrator, "consult_finish", lambda **kw: finished.append(kw) or {})
    monkeypatch.setattr(orchestrator, "consult_status", lambda **kw: {"status": "up"})

    llm = ScriptedLLM()
    llm.replies = [
        # relay reply for the forced-failure outcome
        "Maaf kijiye, property se abhi jawab nahin mila. Hum aapko update karenge.",
    ]

    with TestClient(app) as client:
        app.state.llm = llm
        with client.websocket_connect("/ws/brain") as ws:
            _start_session(ws, "sess-nofail", "persona_customer", {"channel_id": "ast-2"})
            _start_consult_via_turns(ws, llm, "sess-nofail")

            push = _collect_push(ws)
            assert push["done"]["turn_id"].startswith("consult-push-")
            assert "jawab nahin mila" in push["reply"]

            ws.send_json({"type": "session_end", "session_id": "sess-nofail"})

    assert finished == [{"consult_id": "c-201", "outcome": "failed"}]
    assert "[CONSULT RESULT: confirmed=unknown" in llm.calls[-1]["user"]


def test_push_never_interleaves_with_inflight_turn(monkeypatch):
    """Interleaving safety: the result lands while a turn is mid-flight. The
    watcher must NOT emit during that turn; the hold reply completes first and
    the relay push follows as its own complete chunk/done unit."""
    monkeypatch.setattr(ws_handler, "CONSULT_PUSH_POLL_S", 0.05)
    # Patch the HANDLER's imported binding: handler.py does
    # `from app.engine.prompt_agent import derive_consult_push_budget_s`,
    # so patching prompt_agent's symbol never reaches the watcher.
    monkeypatch.setattr(ws_handler, "derive_consult_push_budget_s", lambda **_kw: 5.0)
    finished: list[dict[str, Any]] = []
    monkeypatch.setattr(
        orchestrator,
        "consult_start",
        lambda **kw: {"consult_id": "c-300", "bridge_id": "b-1", "consult_channel_id": "cc-1"},
    )
    monkeypatch.setattr(orchestrator, "consult_finish", lambda **kw: finished.append(kw) or {})

    # consult_status blocks the FIRST call until released: this holds the
    # customer's next turn in-flight (its pending-consult check runs
    # consult_status via a worker thread) for a deterministic window.
    turn_inflight = threading.Event()
    release_turn = threading.Event()
    first_status_call = threading.Event()

    def blocking_status(**kw):
        if not first_status_call.is_set():
            first_status_call.set()
            turn_inflight.set()
            assert release_turn.wait(timeout=5.0), "test never released the blocked turn"
        return {"status": "up"}

    monkeypatch.setattr(orchestrator, "consult_status", blocking_status)

    llm = ScriptedLLM()
    llm.replies = [
        # relay reply for the eventual push
        "Property ne aapki booking BK123 confirm kar di hai.",
    ]

    with TestClient(app) as client:
        app.state.llm = llm
        with client.websocket_connect("/ws/brain") as ws:
            _start_session(ws, "sess-inter", "persona_customer", {"channel_id": "ast-3"})
            _start_consult_via_turns(ws, llm, "sess-inter")

            # Customer speaks; the turn blocks inside its consult_status poll.
            ws.send_json(
                {
                    "type": "turn",
                    "session_id": "sess-inter",
                    "turn_id": "t-mid",
                    "transcript": "hello? kuch pata chala?",
                    "flow_class": "Default",
                }
            )
            assert turn_inflight.wait(timeout=5.0), "turn never reached consult_status"

            # Result lands while t-mid is mid-flight. Give the watcher several
            # poll ticks: it must see the in-flight turn and hold off.
            prompt_agent.CONSULT_RESULTS["BK123"] = {
                "confirmed": "yes",
                "note": "owner confirmed",
            }
            time.sleep(0.3)
            release_turn.set()

            # Strict frame order: ALL t-mid frames (hold reply) first, then the
            # complete consult-push unit. No interleaving.
            frames: list[dict[str, Any]] = []
            deadline = time.monotonic() + 5.0
            done_turns: list[str] = []
            while time.monotonic() < deadline and len(done_turns) < 2:
                msg = json.loads(ws.receive_text())
                frames.append(msg)
                if msg["type"] == "done":
                    done_turns.append(msg["turn_id"])
            assert len(done_turns) == 2, f"expected 2 done frames, got {done_turns}"
            assert done_turns[0] == "t-mid"
            assert done_turns[1].startswith("consult-push-")

            # Every frame before the t-mid done belongs to t-mid; every frame
            # after belongs to the push turn.
            split = next(
                i for i, f in enumerate(frames) if f["type"] == "done" and f["turn_id"] == "t-mid"
            )
            assert all(f["turn_id"] == "t-mid" for f in frames[: split + 1])
            assert all(f["turn_id"] == done_turns[1] for f in frames[split + 1 :])

            # The blocked turn answered with the hold line (result arrived
            # after its check), and the push carried the actual outcome.
            hold_text = " ".join(
                f["text"] for f in frames[: split + 1] if f["type"] == "chunk"
            )
            push_text = " ".join(
                f["text"] for f in frames[split + 1 :] if f["type"] == "chunk"
            )
            assert "intezaar" in hold_text or "line par" in hold_text
            assert "confirm kar di" in push_text

            ws.send_json({"type": "session_end", "session_id": "sess-inter"})

    assert finished == [{"consult_id": "c-300", "outcome": "confirmed=yes"}]


def test_deferred_consult_failure_pushes_fail_line(monkeypatch):
    """consult_start fails AFTER the hold announcement played: the customer
    must hear the could-not-reach line instead of dead air."""

    def fail_consult_start(**kw):
        raise orchestrator.OrchestratorError("480 Temporarily unavailable")

    monkeypatch.setattr(orchestrator, "consult_start", fail_consult_start)

    llm = ScriptedLLM()
    llm.replies = [
        "Theek hai, line par bane rahiye. "
        '<consult booking_id=BK500 hotel="Hotel X" guest=Amit phone=9990001111>',
    ]

    with TestClient(app) as client:
        app.state.llm = llm
        with client.websocket_connect("/ws/brain") as ws:
            _start_session(ws, "sess-cfail", "persona_customer")
            out = _drive_turn(ws, "sess-cfail", "t-1", "haan hold kar sakta hoon")
            assert "<consult" not in out["reply"]

            _send_playback_done(ws, "sess-cfail", "t-1")
            push = _collect_push(ws)
            assert push["done"]["turn_id"].startswith("consult-fail-")
            assert push["done"]["disposition"] == "CONSULT_FAILED"
            assert "contact nahin kar pa raha" in push["reply"]

            ws.send_json({"type": "session_end", "session_id": "sess-cfail"})


def test_end_call_marker_done_carries_playback_grace(monkeypatch):
    """Graceful goodbye: <end_call> ends the turn with end_call=True and the
    configured playback grace so the goodbye audio is never clipped."""
    llm = ScriptedLLM()
    llm.replies = ["OYO choose karne ke liye dhanyavaad, aapka din shubh ho. <end_call>"]

    with TestClient(app) as client:
        app.state.llm = llm
        with client.websocket_connect("/ws/brain") as ws:
            _start_session(ws, "sess-bye", "persona_customer")
            out = _drive_turn(ws, "sess-bye", "t-1", "bas itna hi, dhanyavaad")
            assert "<end_call" not in out["reply"]
            assert out["done"]["end_call"] is True
            assert out["done"]["disposition"] == "COMPLETED"
            assert out["done"]["end_call_delay_ms"] == get_settings().end_call_grace_ms

            ws.send_json({"type": "session_end", "session_id": "sess-bye"})


def test_noinput_reprompts_then_disconnects(monkeypatch):
    """Silence policy: the question is repeated after each unanswered playback
    (2 reprompts = 3 asks total); the 3rd silence pushes the disconnect line
    with end_call and the 3s hangup grace."""
    settings = get_settings()
    monkeypatch.setattr(settings, "noinput_reprompt_s", 0.05)
    monkeypatch.setattr(settings, "noinput_max_reprompts", 2)
    monkeypatch.setattr(settings, "noinput_hangup_delay_ms", 3000)

    llm = ScriptedLLM()
    llm.replies = ["Apna booking ID bataiye?"]

    with TestClient(app) as client:
        app.state.llm = llm
        with client.websocket_connect("/ws/brain") as ws:
            _start_session(ws, "sess-silence", "persona_customer")
            out = _drive_turn(ws, "sess-silence", "t-1", "booking check karni hai")
            assert out["reply"] == "Apna booking ID bataiye?"

            # Question played, then silence -> reprompt 1 (ask #2).
            _send_playback_done(ws, "sess-silence", "t-1")
            push1 = _collect_push(ws)
            assert push1["done"]["turn_id"].startswith("noinput-1-")
            assert push1["done"]["disposition"] == "NOINPUT_REPROMPT"
            assert push1["done"]["end_call"] is False
            assert push1["reply"] == "Apna booking ID bataiye?"

            # Reprompt played, silence again -> reprompt 2 (ask #3).
            _send_playback_done(ws, "sess-silence", push1["done"]["turn_id"])
            push2 = _collect_push(ws)
            assert push2["done"]["turn_id"].startswith("noinput-2-")
            assert push2["reply"] == "Apna booking ID bataiye?"

            # Third silence -> announce + disconnect with 3s grace.
            _send_playback_done(ws, "sess-silence", push2["done"]["turn_id"])
            push3 = _collect_push(ws)
            assert push3["done"]["turn_id"].startswith("noinput-end-")
            assert push3["done"]["disposition"] == "NOINPUT_DISCONNECT"
            assert push3["done"]["end_call"] is True
            assert push3["done"]["end_call_delay_ms"] == 3000
            assert "disconnect" in push3["reply"]

            ws.send_json({"type": "session_end", "session_id": "sess-silence"})


def test_caller_turn_resets_noinput_escalation(monkeypatch):
    """A caller turn between silences resets the reprompt counter — the
    disconnect only fires after 3 CONSECUTIVE unanswered asks."""
    settings = get_settings()
    monkeypatch.setattr(settings, "noinput_reprompt_s", 0.05)
    monkeypatch.setattr(settings, "noinput_max_reprompts", 2)

    llm = ScriptedLLM()
    llm.replies = ["Apna booking ID bataiye?", "Hotel ka naam bataiye?"]

    with TestClient(app) as client:
        app.state.llm = llm
        with client.websocket_connect("/ws/brain") as ws:
            _start_session(ws, "sess-reset", "persona_customer")
            _drive_turn(ws, "sess-reset", "t-1", "booking check karni hai")

            _send_playback_done(ws, "sess-reset", "t-1")
            push1 = _collect_push(ws)
            assert push1["done"]["turn_id"].startswith("noinput-1-")

            # Caller answers: escalation resets and the flow continues.
            out = _drive_turn(ws, "sess-reset", "t-2", "BK123 hai")
            assert out["reply"] == "Hotel ka naam bataiye?"

            # Next silence starts over at reprompt 1, not the disconnect.
            _send_playback_done(ws, "sess-reset", "t-2")
            push2 = _collect_push(ws)
            assert push2["done"]["turn_id"].startswith("noinput-1-")
            assert push2["reply"] == "Hotel ka naam bataiye?"

            ws.send_json({"type": "session_end", "session_id": "sess-reset"})


def test_consult_interim_line_pushed_once(monkeypatch):
    """After dial attempt 1 fails the watcher pushes one interim hold line."""
    monkeypatch.setattr(ws_handler, "CONSULT_PUSH_POLL_S", 0.05)
    # Patch the HANDLER's imported binding: handler.py does
    # `from app.engine.prompt_agent import derive_consult_push_budget_s`,
    # so patching prompt_agent's symbol never reaches the watcher.
    monkeypatch.setattr(ws_handler, "derive_consult_push_budget_s", lambda **_kw: 5.0)
    statuses = [
        {"status": "retrying", "attempt": 2, "max_attempts": 3},
        {"status": "retrying", "attempt": 2, "max_attempts": 3},
        {"status": "up", "attempt": 2, "max_attempts": 3},
    ]
    hold_calls: list[str] = []

    def consult_status(**kw):
        return statuses.pop(0) if statuses else {"status": "up", "attempt": 2, "max_attempts": 3}

    monkeypatch.setattr(orchestrator, "consult_start", lambda **kw: {
        "consult_id": "c-interim",
        "bridge_id": "b-1",
        "consult_channel_id": "cc-1",
    })
    monkeypatch.setattr(orchestrator, "consult_status", consult_status)
    monkeypatch.setattr(orchestrator, "consult_finish", lambda **kw: {})
    monkeypatch.setattr(
        orchestrator,
        "consult_hold_pause",
        lambda **kw: hold_calls.append("pause") or {"status": "hold_paused"},
    )
    monkeypatch.setattr(
        orchestrator,
        "consult_hold_resume",
        lambda **kw: hold_calls.append("resume") or {"status": "hold_resumed"},
    )

    llm = ScriptedLLM()
    llm.replies = ["Ruk jaiye. <consult booking_id=BK123 hotel=X guest=Y phone=9990001111>"]

    with TestClient(app) as client:
        app.state.llm = llm
        with client.websocket_connect("/ws/brain") as ws:
            _start_session(ws, "sess-interim", "persona_customer", {"channel_id": "ast-i1"})
            _start_consult_via_turns(ws, llm, "sess-interim")

            push = _collect_push(ws)
            assert push["done"]["turn_id"].startswith("consult-interim-")
            assert push["done"]["disposition"] == "CONSULT_RETRY_INTERIM"
            assert "दोबारा" in push["reply"]
            assert hold_calls == ["pause"]

            _send_playback_done(ws, "sess-interim", push["done"]["turn_id"])
            _wait_for(lambda: hold_calls == ["pause", "resume"], what="hold resume after playback_done")

            ws.send_json({"type": "session_end", "session_id": "sess-interim"})


def test_watcher_does_not_force_fail_while_retries_remain(monkeypatch):
    """Past the safety budget, the watcher must not push fallback while the
    orchestrator still reports retrying with attempts remaining."""
    monkeypatch.setattr(ws_handler, "CONSULT_PUSH_POLL_S", 0.05)
    # Patch the handler's imported binding (see note in the 5.0s tests).
    monkeypatch.setattr(ws_handler, "derive_consult_push_budget_s", lambda **_kw: 0.08)
    finished: list[dict[str, Any]] = []
    monkeypatch.setattr(
        orchestrator,
        "consult_start",
        lambda **kw: {"consult_id": "c-race", "bridge_id": "b-1", "consult_channel_id": "cc-1"},
    )
    monkeypatch.setattr(
        orchestrator,
        "consult_status",
        lambda **kw: {"status": "retrying", "attempt": 2, "max_attempts": 3},
    )
    monkeypatch.setattr(orchestrator, "consult_finish", lambda **kw: finished.append(kw) or {})

    llm = ScriptedLLM()
    llm.replies = ["Ruk jaiye. <consult booking_id=BK123 hotel=X guest=Y phone=9990001111>"]

    with TestClient(app) as client:
        app.state.llm = llm
        with client.websocket_connect("/ws/brain") as ws:
            _start_session(ws, "sess-race", "persona_customer", {"channel_id": "ast-r1"})
            _start_consult_via_turns(ws, llm, "sess-race")

            # Interim may arrive; no terminal consult-push / finish while
            # retrying. The watcher staying silent is the CORRECT outcome, so
            # reads must be bounded — a bare receive_text() would block forever
            # on that very correctness and hang the suite.
            deadline = time.monotonic() + 1.5
            saw_interim = False
            while time.monotonic() < deadline:
                msg = _receive_json_timeout(ws, timeout_s=0.2)
                if msg is None:
                    if saw_interim:
                        break  # silence after the interim: watcher held back
                    continue
                if msg.get("type") == "done":
                    if msg.get("disposition") == "CONSULT_RETRY_INTERIM":
                        saw_interim = True
                    if str(msg.get("turn_id", "")).startswith("consult-push-"):
                        pytest.fail("watcher pushed fallback while retries remain")
            assert saw_interim
            assert finished == []

            ws.send_json({"type": "session_end", "session_id": "sess-race"})


def test_exhausted_retries_use_scripted_no_answer_reply(monkeypatch):
    """When orchestrator reports no_answer_after_N_attempts, build_consult_relay
    uses the tenant consult_no_answer_reply without calling the LLM."""
    monkeypatch.setattr(ws_handler, "CONSULT_PUSH_POLL_S", 0.05)
    # Patch the handler's imported binding (see note in the 5.0s tests).
    monkeypatch.setattr(ws_handler, "derive_consult_push_budget_s", lambda **_kw: 0.2)
    finished: list[dict[str, Any]] = []
    monkeypatch.setattr(
        orchestrator,
        "consult_start",
        lambda **kw: {"consult_id": "c-exh", "bridge_id": "b-1", "consult_channel_id": "cc-1"},
    )
    monkeypatch.setattr(
        orchestrator,
        "consult_status",
        lambda **kw: {
            "status": "failed",
            "detail": "no_answer_after_3_attempts",
            "attempt": 3,
            "max_attempts": 3,
        },
    )
    monkeypatch.setattr(orchestrator, "consult_finish", lambda **kw: finished.append(kw) or {})

    llm = ScriptedLLM()
    llm.replies = ["Ruk jaiye. <consult booking_id=BK123 hotel=X guest=Y phone=9990001111>"]

    with TestClient(app) as client:
        app.state.llm = llm
        with client.websocket_connect("/ws/brain") as ws:
            _start_session(ws, "sess-exh", "persona_customer", {"channel_id": "ast-e1"})
            _start_consult_via_turns(ws, llm, "sess-exh")

            push = _collect_push(ws)
            assert push["done"]["turn_id"].startswith("consult-push-")
            assert push["done"]["disposition"] == "CONSULT_RELAYED"
            assert "प्रॉपर्टी से अभी संपर्क" in push["reply"]
            assert "कॉल" in push["reply"]

            ws.send_json({"type": "session_end", "session_id": "sess-exh"})

    assert finished == [{"consult_id": "c-exh", "outcome": "failed"}]
    # Only the consult-STARTING turn hit the LLM; the relay itself used the
    # scripted tenant no-answer reply with NO LLM call (no "[CONSULT RESULT"
    # relay prompt was ever built).
    assert len(llm.calls) == 1
    assert "[CONSULT RESULT" not in llm.calls[0]["user"]
