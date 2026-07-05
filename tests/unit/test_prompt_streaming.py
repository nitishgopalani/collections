"""Streaming prompt-mode tests (mocked streaming LLM, mocked orchestrator).

Two layers:

* Agent-level: handle_prompt_turn_streaming with an async-generator LLM double
  — proves sentences are emitted BEFORE the token stream finishes, markers
  split across token boundaries still parse, and cancellation closes the
  stream without leaking state.
* WS-level: the real /ws/brain endpoint with the booking-confirm tenant
  (streaming_llm=True) — proves the chunk/flow_class/done contract, seq order,
  first-chunk-before-stream-end, and cancel mid-stream.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import pytest
from starlette.testclient import TestClient

from app.clients import orchestrator
from app.config import tenant_config
from app.engine import prompt_agent
from app.engine.prompt_agent import handle_prompt_turn_streaming
from app.main import app
from app.ws.session import BrainWSSession


class StreamingScriptedLLM:
    """LLM double: stream() yields queued tokens one by one.

    Records lifecycle events so tests can assert interleaving:
    ("token", i) appended AFTER token i is yielded to the consumer,
    stream_finished set when the generator ran to completion, stream_closed
    set when the generator was closed early (cancel/abort).
    """

    def __init__(self, tokens: list[str]) -> None:
        self.tokens = list(tokens)
        self.events: list[tuple[str, Any]] = []
        self.calls: list[dict[str, str]] = []
        self.stream_finished = False
        self.stream_closed = False
        # Optional gate: the stream blocks before yielding tokens[gate_after:]
        # until the test releases it (thread-safe for TestClient tests).
        self.gate_after: int | None = None
        self.gate = threading.Event()

    async def stream(self, system: str, user: str):
        self.calls.append({"system": system, "user": user})
        try:
            for i, tok in enumerate(self.tokens):
                if self.gate_after is not None and i == self.gate_after:
                    released = await asyncio.to_thread(self.gate.wait, 5.0)
                    assert released, "test never released the stream gate"
                yield tok
                self.events.append(("token", i))
            self.stream_finished = True
        finally:
            if not self.stream_finished:
                self.stream_closed = True

    async def complete(self, system: str, user: str, **kw: Any) -> str:
        raise AssertionError("streaming path must not call complete()")

    async def ping(self) -> bool:  # pragma: no cover - health endpoint only
        return True


def make_session(session_id: str = "sess-st", agent_id: str = "persona_customer", **ctx: str):
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


def test_streaming_flag_is_booking_confirm_only():
    assert tenant_config("booking-confirm").streaming_llm is True
    assert tenant_config("salary_on_time").streaming_llm is False
    assert tenant_config("default").streaming_llm is False


# --------------------------------------------------------------------------
# Agent level
# --------------------------------------------------------------------------


async def test_first_sentence_emitted_before_stream_finishes(tenant_cfg):
    """THE point of streaming: sentence 1 reaches the sink while the LLM is
    still producing tokens for the rest of the reply."""
    llm = StreamingScriptedLLM(
        ["Namaste! ", "Main aapki ", "kya madad ", "kar sakta hoon? ", "Booking ID bataiye."]
    )
    events = llm.events  # sentences interleave into the same list

    async def on_sentence(text: str) -> None:
        events.append(("sentence", text))

    result = await handle_prompt_turn_streaming(
        session=make_session(),
        transcript="hello",
        llm=llm,
        tenant_cfg=tenant_cfg,
        on_sentence=on_sentence,
    )

    assert llm.stream_finished
    sentences = [e for e in events if e[0] == "sentence"]
    assert [s[1] for s in sentences] == [
        "Namaste!",
        "Main aapki kya madad kar sakta hoon?",
        "Booking ID bataiye.",
    ]
    # Sentence 1 was emitted right after token 0, before tokens 1..4 streamed.
    first_sentence_pos = events.index(("sentence", "Namaste!"))
    last_token_pos = events.index(("token", len(llm.tokens) - 1))
    assert first_sentence_pos < last_token_pos, (
        f"first sentence must be emitted mid-stream, got order: {events}"
    )
    assert result.reply_text == "Namaste! Main aapki kya madad kar sakta hoon? Booking ID bataiye."
    # History recorded exactly what was spoken.
    assert prompt_agent.session_history("sess-st")[-1]["text"] == result.reply_text


async def test_consult_marker_split_across_tokens_still_parses(tenant_cfg, monkeypatch):
    started: list[dict[str, Any]] = []
    monkeypatch.setattr(
        orchestrator,
        "consult_start",
        lambda **kw: started.append(kw)
        or {"consult_id": "c-1", "bridge_id": "b-1", "consult_channel_id": "cc-1"},
    )
    llm = StreamingScriptedLLM(
        [
            "Main property se confirm ",
            "karke batata hoon, line par bane rahiye. ",
            "<consult booking_id=BK1",
            '23 hotel="Hotel Sun',
            'rise" guest=Rahul ',
            "phone=9990001111>",
        ]
    )
    spoken: list[str] = []

    async def on_sentence(text: str) -> None:
        spoken.append(text)

    session = make_session()
    result = await handle_prompt_turn_streaming(
        session=session,
        transcript="BK123 Hotel Sunrise Rahul",
        llm=llm,
        tenant_cfg=tenant_cfg,
        on_sentence=on_sentence,
    )
    # Marker parsed whole despite arriving in 4 pieces; never spoken. The
    # orchestrator is NOT called during the turn — the request is deferred
    # until the announcement's playback_done.
    assert started == []
    assert result.consult_request == {
        "booking_id": "BK123",
        "hotel": "Hotel Sunrise",
        "guest": "Rahul",
        "phone": "9990001111",
    }
    assert spoken == ["Main property se confirm karke batata hoon, line par bane rahiye."]
    assert all("<consult" not in s for s in spoken)
    assert prompt_agent._SESSIONS["sess-st"].pending is None
    assert result.end_call is False

    # Deferred start (post-playback) dials the property.
    assert await prompt_agent.start_deferred_consult(session, result.consult_request)
    assert started == [
        {
            "session_uuid": "sess-st",
            "consult_destination": "9990001111",
            "caller_id": "",
        }
    ]
    assert prompt_agent._SESSIONS["sess-st"].pending is not None


async def test_consult_result_marker_split_across_tokens(tenant_cfg):
    llm = StreamingScriptedLLM(
        [
            "Bahut shukriya, aapka din shubh ho. ",
            "<consult_result booking_id=BK1",
            "23 confirmed=yes ",
            'note="owner confirmed">',
        ]
    )
    spoken: list[str] = []

    async def on_sentence(text: str) -> None:
        spoken.append(text)

    result = await handle_prompt_turn_streaming(
        session=make_session("sess-prop", agent_id="persona_property"),
        transcript="haan confirm hai",
        llm=llm,
        tenant_cfg=tenant_cfg,
        on_sentence=on_sentence,
    )
    assert result.end_call is True
    assert result.disposition == "CONSULT_REPORTED"
    assert spoken == ["Bahut shukriya, aapka din shubh ho."]
    assert prompt_agent.CONSULT_RESULTS["BK123"] == {
        "confirmed": "yes",
        "note": "owner confirmed",
    }


async def test_cancel_mid_stream_closes_llm_and_leaves_no_pending_state(tenant_cfg):
    """Barge-in: the turn task is cancelled mid-stream. The LLM stream must be
    closed (not leaked) and no partial turn state may linger."""
    llm = StreamingScriptedLLM(["Pehla vakya hai. ", "Doosra vakya ", "kabhi poora nahi hota."])
    llm.gate_after = 1  # stream blocks after the first token until released
    spoken: list[str] = []
    first_sentence_out = asyncio.Event()

    async def on_sentence(text: str) -> None:
        spoken.append(text)
        first_sentence_out.set()

    task = asyncio.create_task(
        handle_prompt_turn_streaming(
            session=make_session(),
            transcript="hello",
            llm=llm,
            tenant_cfg=tenant_cfg,
            on_sentence=on_sentence,
        )
    )
    await asyncio.wait_for(first_sentence_out.wait(), timeout=5.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    llm.gate.set()  # unblock the generator so closure can complete

    for _ in range(100):
        if llm.stream_closed:
            break
        await asyncio.sleep(0.02)
    assert llm.stream_closed, "cancel must close the LLM stream"
    assert not llm.stream_finished
    assert spoken == ["Pehla vakya hai."]
    # No half-recorded assistant turn: the cancelled turn never reached the
    # history append (only the booking-context-free empty state exists).
    history = prompt_agent.session_history("sess-st")
    assert all(entry["role"] != "assistant" for entry in history)
    state = prompt_agent._SESSIONS.get("sess-st")
    assert state is None or state.pending is None


async def test_llm_error_before_output_speaks_fallback(tenant_cfg):
    class BoomStreamLLM:
        async def stream(self, system: str, user: str):
            raise RuntimeError("vertex down")
            yield  # pragma: no cover - makes this an async generator

    spoken: list[str] = []

    async def on_sentence(text: str) -> None:
        spoken.append(text)

    result = await handle_prompt_turn_streaming(
        session=make_session(),
        transcript="hello",
        llm=BoomStreamLLM(),
        tenant_cfg=tenant_cfg,
        on_sentence=on_sentence,
    )
    assert "technical dikkat" in result.reply_text
    assert spoken == [result.reply_text]


async def test_empty_stream_speaks_safe_fallback(tenant_cfg):
    llm = StreamingScriptedLLM([])
    spoken: list[str] = []

    async def on_sentence(text: str) -> None:
        spoken.append(text)

    result = await handle_prompt_turn_streaming(
        session=make_session(),
        transcript="hello",
        llm=llm,
        tenant_cfg=tenant_cfg,
        on_sentence=on_sentence,
    )
    assert spoken == [tenant_cfg.safe_fallback_reply]
    assert result.reply_text == tenant_cfg.safe_fallback_reply


# --------------------------------------------------------------------------
# WS level (real /ws/brain endpoint, booking-confirm tenant, streaming flag on)
# --------------------------------------------------------------------------


def _start_session(ws, session_id: str, agent_id: str = "persona_customer"):
    ws.send_json(
        {
            "type": "session_start",
            "session_id": session_id,
            "borrower_id": "caller-1",
            "agent_id": agent_id,
            "client_id": "booking-confirm",
            "borrower_context": {},
        }
    )
    ready = json.loads(ws.receive_text())
    assert ready["type"] == "session_ready"


def test_ws_first_chunk_arrives_before_stream_finishes():
    """End-to-end latency win: chunk seq=0 is on the wire while the LLM double
    is still gated mid-stream."""
    llm = StreamingScriptedLLM(
        ["Namaste! ", "OYO support mein swagat hai. ", "Booking ID bataiye."]
    )
    llm.gate_after = 1  # tokens 1.. wait until the test releases the gate

    with TestClient(app) as client:
        app.state.llm = llm
        with client.websocket_connect("/ws/brain") as ws:
            _start_session(ws, "sess-ws-first")
            ws.send_json(
                {
                    "type": "turn",
                    "session_id": "sess-ws-first",
                    "turn_id": "t-1",
                    "transcript": "hello",
                    "flow_class": "Default",
                }
            )
            first = json.loads(ws.receive_text())
            # The stream is still gated: the LLM has NOT finished, yet the
            # first chunk is already here. This is the whole point.
            assert first == {"type": "chunk", "turn_id": "t-1", "seq": 0, "text": "Namaste!"}
            assert llm.stream_finished is False

            llm.gate.set()
            frames = [first]
            while frames[-1]["type"] != "done":
                frames.append(json.loads(ws.receive_text()))

            chunks = [f for f in frames if f["type"] == "chunk"]
            assert [c["seq"] for c in chunks] == list(range(len(chunks)))
            assert [c["text"] for c in chunks] == [
                "Namaste!",
                "OYO support mein swagat hai.",
                "Booking ID bataiye.",
            ]
            assert [f["type"] for f in frames].count("done") == 1
            assert frames[-2]["type"] == "flow_class"
            assert frames[-1]["end_call"] is False
            assert llm.stream_finished is True

            ws.send_json({"type": "session_end", "session_id": "sess-ws-first"})

    # complete() was never used — the streaming path answered.
    assert len(llm.calls) == 1


def test_ws_cancel_mid_stream_no_done_and_next_turn_clean():
    """Barge-in over the wire: cancel lands mid-stream. The cancelled turn
    emits no further frames (no done), the LLM stream is closed, and the next
    turn starts from a clean slate with its own seq=0."""
    llm = StreamingScriptedLLM(
        ["Pehla vakya hai. ", "Doosra vakya ", "kabhi poora nahi hota."]
    )
    llm.gate_after = 1

    with TestClient(app) as client:
        app.state.llm = llm
        with client.websocket_connect("/ws/brain") as ws:
            _start_session(ws, "sess-ws-cancel")
            ws.send_json(
                {
                    "type": "turn",
                    "session_id": "sess-ws-cancel",
                    "turn_id": "t-1",
                    "transcript": "hello",
                    "flow_class": "Default",
                }
            )
            first = json.loads(ws.receive_text())
            assert first["type"] == "chunk" and first["seq"] == 0

            ws.send_json(
                {"type": "cancel", "session_id": "sess-ws-cancel", "turn_id": "t-1"}
            )
            # Wait for the abort to close the LLM stream (no leak).
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not llm.stream_closed:
                time.sleep(0.02)
            llm.gate.set()
            assert llm.stream_closed, "cancel must abort the in-flight LLM stream"
            assert llm.stream_finished is False

            # Next turn: fresh stream double, clean seq numbering, done arrives.
            llm2 = StreamingScriptedLLM(["Theek hai. ", "Booking ID bataiye."])
            app.state.llm = llm2
            ws.send_json(
                {
                    "type": "turn",
                    "session_id": "sess-ws-cancel",
                    "turn_id": "t-2",
                    "transcript": "booking check karo",
                    "flow_class": "Default",
                }
            )
            frames: list[dict[str, Any]] = []
            while not frames or frames[-1]["type"] != "done":
                frames.append(json.loads(ws.receive_text()))
            # Every frame after the cancel belongs to t-2 — t-1 emitted nothing
            # more (no chunk, no flow_class, no done).
            assert all(f["turn_id"] == "t-2" for f in frames), frames
            chunks = [f for f in frames if f["type"] == "chunk"]
            assert [c["seq"] for c in chunks] == [0, 1]
            assert [c["text"] for c in chunks] == ["Theek hai.", "Booking ID bataiye."]

            ws.send_json({"type": "session_end", "session_id": "sess-ws-cancel"})


def test_ws_consult_stream_end_call_done(monkeypatch):
    """Property leg over the wire: consult_result marker split across tokens
    -> stripped from speech, recorded, and done carries end_call=True."""
    llm = StreamingScriptedLLM(
        [
            "Bahut shukriya, aapka din ",
            "shubh ho. ",
            "<consult_result booking_id=BK7",
            "7 confirmed=no ",
            'note="rooms full">',
        ]
    )
    with TestClient(app) as client:
        app.state.llm = llm
        with client.websocket_connect("/ws/brain") as ws:
            _start_session(ws, "sess-ws-prop", agent_id="persona_property")
            ws.send_json(
                {
                    "type": "turn",
                    "session_id": "sess-ws-prop",
                    "turn_id": "t-1",
                    "transcript": "nahi bhai, rooms full hain",
                    "flow_class": "Default",
                }
            )
            frames: list[dict[str, Any]] = []
            while not frames or frames[-1]["type"] != "done":
                frames.append(json.loads(ws.receive_text()))
            chunks = [f for f in frames if f["type"] == "chunk"]
            assert [c["text"] for c in chunks] == ["Bahut shukriya, aapka din shubh ho."]
            assert all("<consult_result" not in c["text"] for c in chunks)
            done = frames[-1]
            assert done["end_call"] is True
            assert done["disposition"] == "CONSULT_REPORTED"
            assert prompt_agent.CONSULT_RESULTS["BK77"] == {
                "confirmed": "no",
                "note": "rooms full",
            }
            ws.send_json({"type": "session_end", "session_id": "sess-ws-prop"})
