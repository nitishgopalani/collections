"""Prompt-mode agent — ASR text straight to the LLM, reply straight to TTS.

Second agent mode next to the flow engine (selected per tenant via
``TenantConfig.agent_mode == "prompt"``). No flow packs, no borrower store:
per-session conversation history is kept in-memory here, the tenant's persona
system prompt comes from config, and the reply is returned to the WS handler
which emits it through the SAME chunk/flow_class/done contract the flow engine
uses — the connector/go-server sees no difference.

Cross-leg consult hand-off (booking-confirm bot):

* The CUSTOMER persona signals a consult by ending a reply with a structured
  marker ``<consult booking_id=... hotel=... guest=...>``. We strip it, call the
  ari-orchestrator (hold customer + dial the property), and hold the customer
  with a "please stay on the line" reply.
* The PROPERTY persona (its own session, on the consult leg) reports the
  owner's answer with ``<consult_result booking_id=... confirmed=yes|no
  note=...>``. We strip it and record the outcome in :data:`CONSULT_RESULTS`
  keyed by booking_id (the consult correlation id shared by both legs).
* On the customer's next turn we inject ``[CONSULT RESULT: ...]`` as a system
  message so the LLM relays the outcome naturally, and finish the consult.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.engine.stream_sentences import SentenceStreamSplitter

logger = logging.getLogger(__name__)

# How many history entries (user+assistant+system lines) to keep per session.
_MAX_HISTORY_ENTRIES = 60
# How many customer turns we wait on a silent consult before giving up.
_MAX_CONSULT_POLLS = 3

_HOLD_REPLY = (
    "Ji, bas thoda sa aur intezaar kijiye — main property se baat kar raha hoon, "
    "please line par bane rahiye."
)
_CONSULT_FAIL_REPLY = (
    "Maaf kijiye, main abhi property se contact nahin kar pa raha hoon. "
    "Hum aapko thodi der mein update kar denge."
)
_LLM_FAIL_REPLY = "Maaf kijiye, thodi technical dikkat aa gayi. Kya aap dobara bol sakte hain?"

_CONSULT_MARKER_RE = re.compile(r"<consult\s+([^>]*)>", re.IGNORECASE)
_CONSULT_RESULT_MARKER_RE = re.compile(r"<consult_result\s+([^>]*)>", re.IGNORECASE)
_ATTR_RE = re.compile(r'(\w+)=(?:"([^"]*)"|([^\s>]+))')


@dataclass
class PromptTurnResult:
    """Outcome of one prompt-mode turn, consumed by the WS handler."""

    reply_text: str
    end_call: bool = False
    disposition: str | None = None


@dataclass
class _PendingConsult:
    consult_id: str
    booking_id: str
    polls: int = 0


@dataclass
class _SessionState:
    persona: str
    history: list[dict[str, str]] = field(default_factory=list)
    pending: _PendingConsult | None = None


# In-memory per-session prompt-mode state, keyed by session_id.
_SESSIONS: dict[str, _SessionState] = {}

# Cross-leg shared state: booking_id -> {"confirmed": "yes|no|unknown", "note": str}.
# Written by the PROPERTY leg's session when its LLM emits <consult_result ...>;
# read (and consumed) by the CUSTOMER leg's session that triggered the consult.
CONSULT_RESULTS: dict[str, dict[str, str]] = {}


def reset_state() -> None:
    """Clear all prompt-mode state (test isolation)."""
    from app.engine import consult_binding

    _SESSIONS.clear()
    CONSULT_RESULTS.clear()
    consult_binding.reset()


def clear_session(session_id: str) -> None:
    """Drop one session's in-memory history (called on session_end/disconnect)."""
    _SESSIONS.pop(session_id, None)


def session_history(session_id: str) -> list[dict[str, str]]:
    """Read-only copy of a session's conversation history (tests/debugging)."""
    state = _SESSIONS.get(session_id)
    return list(state.history) if state else []


def _parse_marker_attrs(raw: str) -> dict[str, str]:
    return {
        m.group(1).lower(): (m.group(2) or m.group(3) or "").strip()
        for m in _ATTR_RE.finditer(raw)
    }


def _resolve_persona(session: Any, tenant_cfg: Any) -> tuple[str, str]:
    """Pick the persona system prompt: agent_id when it names one, else default."""
    personas: dict[str, str] = tenant_cfg.prompt_personas or {}
    name = (session.agent_id or "").strip()
    if name not in personas:
        name = tenant_cfg.default_persona
    return name, personas.get(name, "")


def _render_user_prompt(history: list[dict[str, str]], transcript: str) -> str:
    """Serialize history + the new caller turn for a single-string LLM call."""
    lines: list[str] = []
    for entry in history:
        role = entry["role"].upper()
        lines.append(f"{role}: {entry['text']}")
    turn = transcript.strip() or "[CALL CONNECTED — greet and start the conversation]"
    lines.append(f"USER: {turn}")
    lines.append("ASSISTANT:")
    return "\n".join(lines)


def _append_history(state: _SessionState, role: str, text: str) -> None:
    state.history.append({"role": role, "text": text})
    if len(state.history) > _MAX_HISTORY_ENTRIES:
        del state.history[: len(state.history) - _MAX_HISTORY_ENTRIES]


def _booking_context_line(borrower_context: dict[str, Any]) -> str:
    """Booking details for the PROPERTY leg, injected as the first system line."""
    keys = ("booking_id", "hotel", "guest", "checkin", "checkin_date")
    parts = [f"{k}={borrower_context[k]}" for k in keys if borrower_context.get(k)]
    if not parts:
        return ""
    return "BOOKING TO VERIFY: " + ", ".join(str(p) for p in parts)


async def _start_consult(session: Any, attrs: dict[str, str]) -> _PendingConsult:
    """Call the orchestrator to hold the customer and dial the property leg."""
    from app.clients import orchestrator
    from app.engine import consult_binding

    destination = attrs.get("phone") or os.getenv("CONSULT_PROPERTY_NUMBER", "")
    if not destination:
        raise orchestrator.OrchestratorError("no consult destination configured")
    # The customer is referenced by this session's own id: it IS the AudioSocket
    # uuid the orchestrator minted for the Stasis-inbound call, so its registry
    # resolves it to the real channel/bridge (no Asterisk channel id needed).
    out = await asyncio.to_thread(
        orchestrator.consult_start,
        session_uuid=str(session.session_id),
        consult_destination=destination,
        caller_id=os.getenv("CONSULT_CALLER_ID", ""),
    )
    # The consult leg's AI leg gets its own brain session under consult_uuid.
    # Pre-register the property persona + booking context for it so the WS
    # handler binds that session correctly the moment it starts.
    consult_uuid = str(out.get("consult_uuid", ""))
    if consult_uuid:
        consult_binding.register(
            consult_uuid,
            {
                "tenant_id": str(session.tenant_id),
                "persona": "persona_property",
                "booking_id": attrs.get("booking_id", ""),
                "hotel": attrs.get("hotel", ""),
                "guest": attrs.get("guest", ""),
                "checkin": attrs.get("checkin", ""),
            },
        )
    return _PendingConsult(
        consult_id=str(out.get("consult_id", "")),
        booking_id=attrs.get("booking_id", ""),
    )


async def _finish_consult(consult_id: str, outcome: str) -> None:
    from app.clients import orchestrator

    if not consult_id:
        return
    try:
        await asyncio.to_thread(orchestrator.consult_finish, consult_id=consult_id, outcome=outcome)
    except orchestrator.OrchestratorError:
        logger.warning("prompt_agent consult_finish failed consult_id=%s", consult_id)


async def _poll_consult_failed(consult_id: str) -> bool:
    """True when the orchestrator reports the consult leg failed (e.g. telco 480)."""
    from app.clients import orchestrator

    if not consult_id:
        return False
    try:
        out = await asyncio.to_thread(orchestrator.consult_status, consult_id=consult_id)
    except orchestrator.OrchestratorError:
        return False
    return str(out.get("status", "")) == "failed"


async def _take_result(state: _SessionState, *, force_fail: bool = False) -> dict[str, str] | None:
    """Consume the consult outcome if it is decided; None means still waiting.

    Decided means: the property leg posted a result to CONSULT_RESULTS, the
    orchestrator reports the consult leg failed, or force_fail (caller's wait
    budget exhausted). Consuming clears state.pending and finishes the consult.
    """
    pending = state.pending
    if pending is None:
        return None
    result = CONSULT_RESULTS.pop(pending.booking_id, None)
    if result is not None:
        state.pending = None
        await _finish_consult(pending.consult_id, f"confirmed={result.get('confirmed', '')}")
        return result
    if force_fail or await _poll_consult_failed(pending.consult_id):
        state.pending = None
        await _finish_consult(pending.consult_id, "failed")
        return {"confirmed": "unknown", "note": "could not reach the property"}
    return None


async def _resolve_pending_consult(state: _SessionState) -> dict[str, str] | None:
    """Turn-driven check of the pending consult; None means keep holding."""
    pending = state.pending
    if pending is None:
        return None
    result = await _take_result(state)
    if result is not None:
        return result
    pending.polls += 1
    if pending.polls > _MAX_CONSULT_POLLS:
        return await _take_result(state, force_fail=True)
    return None


def has_pending_consult(session_id: str) -> bool:
    """True while this session has a consult awaiting its result."""
    state = _SESSIONS.get(session_id)
    return state is not None and state.pending is not None


async def take_consult_result(
    session_id: str, *, force_fail: bool = False
) -> dict[str, str] | None:
    """Watcher-driven check: consume the consult outcome if decided, else None.

    Unlike the turn path this does NOT count polls — the caller (the WS
    handler's consult watcher) owns its own time budget and passes
    force_fail=True when that budget is exhausted.
    """
    state = _SESSIONS.get(session_id)
    if state is None:
        return None
    return await _take_result(state, force_fail=force_fail)


async def build_consult_relay(
    *,
    session: Any,
    llm: Any,
    tenant_cfg: Any,
    result: dict[str, str],
) -> str:
    """Turn a consult outcome into the reply relayed to the waiting customer.

    Injects the outcome into the session history as a system line (same shape
    the turn path uses) and asks the persona LLM for a natural relay. Used by
    the unsolicited push path where no caller transcript exists.
    """
    persona_name, system_prompt = _resolve_persona(session, tenant_cfg)
    state = _SESSIONS.get(session.session_id)
    if state is None:
        state = _SessionState(persona=persona_name)
        _SESSIONS[session.session_id] = state
    _append_history(
        state,
        "system",
        f"[CONSULT RESULT: confirmed={result.get('confirmed', 'unknown')}, "
        f"note={result.get('note', '')}]",
    )
    fallback = (
        _CONSULT_FAIL_REPLY
        if result.get("confirmed", "unknown") == "unknown"
        else "Aapki booking ke baare mein property se jawab aa gaya hai: "
        f"confirmed={result.get('confirmed', '')}."
    )
    if not system_prompt:
        _append_history(state, "assistant", fallback)
        return fallback
    user_prompt = _render_user_prompt(
        state.history,
        "[CONSULT RESULT ARRIVED — relay the outcome to the waiting customer now]",
    )
    try:
        raw_reply = await llm.complete(system_prompt, user_prompt, json_only=False)
    except Exception:
        logger.exception(
            "prompt_agent consult relay LLM call failed session_id=%s persona=%s",
            session.session_id,
            persona_name,
        )
        raw_reply = ""
    reply = (raw_reply or "").strip() or fallback
    _append_history(state, "assistant", reply)
    return reply


async def handle_prompt_turn(
    *,
    session: Any,
    transcript: str,
    llm: Any,
    tenant_cfg: Any,
) -> PromptTurnResult:
    """Run one prompt-mode turn: history + persona prompt -> LLM -> reply text."""
    persona_name, system_prompt = _resolve_persona(session, tenant_cfg)
    if not system_prompt:
        logger.error(
            "prompt_agent: no persona prompt tenant_id=%s agent_id=%s",
            session.tenant_id,
            session.agent_id,
        )
        return PromptTurnResult(reply_text=tenant_cfg.safe_fallback_reply, end_call=True)

    state = _SESSIONS.get(session.session_id)
    if state is None:
        state = _SessionState(persona=persona_name)
        _SESSIONS[session.session_id] = state
        booking_line = _booking_context_line(session.borrower_context or {})
        if booking_line:
            _append_history(state, "system", booking_line)

    # Consult in flight (customer leg): inject the property outcome when it has
    # arrived, otherwise keep the caller on the line without an LLM round-trip.
    if state.pending is not None:
        result = await _resolve_pending_consult(state)
        if result is None:
            if transcript.strip():
                _append_history(state, "user", transcript.strip())
            _append_history(state, "assistant", _HOLD_REPLY)
            return PromptTurnResult(reply_text=_HOLD_REPLY)
        _append_history(
            state,
            "system",
            f"[CONSULT RESULT: confirmed={result.get('confirmed', 'unknown')}, "
            f"note={result.get('note', '')}]",
        )

    user_prompt = _render_user_prompt(state.history, transcript)
    try:
        raw_reply = await llm.complete(system_prompt, user_prompt, json_only=False)
    except Exception:
        logger.exception(
            "prompt_agent LLM call failed session_id=%s persona=%s",
            session.session_id,
            persona_name,
        )
        return PromptTurnResult(reply_text=_LLM_FAIL_REPLY)
    reply = (raw_reply or "").strip()

    end_call = False
    disposition: str | None = None

    # PROPERTY leg: the owner answered — record the outcome for the customer leg.
    result_match = _CONSULT_RESULT_MARKER_RE.search(reply)
    if result_match:
        attrs = _parse_marker_attrs(result_match.group(1))
        booking_id = attrs.get("booking_id", "")
        if booking_id:
            CONSULT_RESULTS[booking_id] = {
                "confirmed": attrs.get("confirmed", "unknown"),
                "note": attrs.get("note", ""),
            }
            logger.info(
                "prompt_agent consult result recorded booking_id=%s confirmed=%s",
                booking_id,
                attrs.get("confirmed", ""),
            )
        reply = _CONSULT_RESULT_MARKER_RE.sub("", reply).strip()
        end_call = True
        disposition = "CONSULT_REPORTED"

    # CUSTOMER leg: the LLM asked for a property consult — hold + dial.
    consult_match = _CONSULT_MARKER_RE.search(reply)
    if consult_match and not result_match:
        attrs = _parse_marker_attrs(consult_match.group(1))
        reply = _CONSULT_MARKER_RE.sub("", reply).strip()
        from app.clients.orchestrator import OrchestratorError

        try:
            state.pending = await _start_consult(session, attrs)
            logger.info(
                "prompt_agent consult started session_id=%s consult_id=%s booking_id=%s",
                session.session_id,
                state.pending.consult_id,
                state.pending.booking_id,
            )
            if not reply:
                reply = _HOLD_REPLY
        except OrchestratorError:
            logger.exception(
                "prompt_agent consult_start failed session_id=%s", session.session_id
            )
            reply = _CONSULT_FAIL_REPLY

    if not reply:
        reply = tenant_cfg.safe_fallback_reply

    if transcript.strip():
        _append_history(state, "user", transcript.strip())
    _append_history(state, "assistant", reply)
    return PromptTurnResult(reply_text=reply, end_call=end_call, disposition=disposition)


async def handle_prompt_turn_streaming(
    *,
    session: Any,
    transcript: str,
    llm: Any,
    tenant_cfg: Any,
    on_sentence: Callable[[str], Awaitable[None]],
) -> PromptTurnResult:
    """Streaming prompt-mode turn (tenant flag ``streaming_llm``).

    Consumes ``llm.stream(system, user)`` token-by-token through a
    sentence-boundary splitter. Each completed sentence gets the SAME marker
    parsing the non-streaming path applies to the whole reply, then goes out
    immediately via ``on_sentence`` — so the first sentence reaches TTS while
    the LLM is still generating. Everything spoken is delivered through
    ``on_sentence``; the returned ``reply_text`` is the joined transcript for
    history/logging only (the caller must NOT re-emit it).

    Cancellation (barge-in/supersede) raises :class:`asyncio.CancelledError`
    into the ``async for``; the ``finally`` closes the LLM stream so the
    underlying Vertex stream is aborted, not leaked.

    Streaming-specific divergences from the non-streaming path (sentences
    already spoken cannot be unsaid):

    * consult_start failure appends :data:`_CONSULT_FAIL_REPLY` instead of
      replacing the whole reply with it.
    * An LLM error mid-stream ends the turn with what was already spoken;
      :data:`_LLM_FAIL_REPLY` is spoken only when nothing got out.
    """
    persona_name, system_prompt = _resolve_persona(session, tenant_cfg)
    if not system_prompt:
        logger.error(
            "prompt_agent: no persona prompt tenant_id=%s agent_id=%s",
            session.tenant_id,
            session.agent_id,
        )
        await on_sentence(tenant_cfg.safe_fallback_reply)
        return PromptTurnResult(reply_text=tenant_cfg.safe_fallback_reply, end_call=True)

    state = _SESSIONS.get(session.session_id)
    if state is None:
        state = _SessionState(persona=persona_name)
        _SESSIONS[session.session_id] = state
        booking_line = _booking_context_line(session.borrower_context or {})
        if booking_line:
            _append_history(state, "system", booking_line)

    # Consult in flight: same pre-LLM short-circuit as the non-streaming path.
    if state.pending is not None:
        result = await _resolve_pending_consult(state)
        if result is None:
            if transcript.strip():
                _append_history(state, "user", transcript.strip())
            _append_history(state, "assistant", _HOLD_REPLY)
            await on_sentence(_HOLD_REPLY)
            return PromptTurnResult(reply_text=_HOLD_REPLY)
        _append_history(
            state,
            "system",
            f"[CONSULT RESULT: confirmed={result.get('confirmed', 'unknown')}, "
            f"note={result.get('note', '')}]",
        )

    user_prompt = _render_user_prompt(state.history, transcript)

    splitter = SentenceStreamSplitter()
    spoken: list[str] = []
    end_call = False
    disposition: str | None = None
    consult_started = False

    async def _speak(text: str) -> None:
        spoken.append(text)
        await on_sentence(text)

    async def _handle_sentence(sentence: str) -> None:
        """Existing marker parsing, applied to ONE completed sentence."""
        nonlocal end_call, disposition, consult_started
        text = sentence

        # PROPERTY leg: the owner answered — record the outcome.
        result_match = _CONSULT_RESULT_MARKER_RE.search(text)
        if result_match:
            attrs = _parse_marker_attrs(result_match.group(1))
            booking_id = attrs.get("booking_id", "")
            if booking_id:
                CONSULT_RESULTS[booking_id] = {
                    "confirmed": attrs.get("confirmed", "unknown"),
                    "note": attrs.get("note", ""),
                }
                logger.info(
                    "prompt_agent consult result recorded booking_id=%s confirmed=%s",
                    booking_id,
                    attrs.get("confirmed", ""),
                )
            text = _CONSULT_RESULT_MARKER_RE.sub("", text).strip()
            end_call = True
            disposition = "CONSULT_REPORTED"

        # CUSTOMER leg: the LLM asked for a property consult — hold + dial.
        consult_match = _CONSULT_MARKER_RE.search(text)
        if consult_match and not result_match:
            attrs = _parse_marker_attrs(consult_match.group(1))
            text = _CONSULT_MARKER_RE.sub("", text).strip()
            from app.clients.orchestrator import OrchestratorError

            try:
                state.pending = await _start_consult(session, attrs)
                consult_started = True
                logger.info(
                    "prompt_agent consult started session_id=%s consult_id=%s booking_id=%s",
                    session.session_id,
                    state.pending.consult_id,
                    state.pending.booking_id,
                )
            except OrchestratorError:
                logger.exception(
                    "prompt_agent consult_start failed session_id=%s", session.session_id
                )
                text = ""
                await _speak(_CONSULT_FAIL_REPLY)

        if text:
            await _speak(text)

    try:
        stream = llm.stream(system_prompt, user_prompt)
        try:
            async for token in stream:
                for sentence in splitter.push(token):
                    await _handle_sentence(sentence)
        finally:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                await aclose()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "prompt_agent streaming LLM call failed session_id=%s persona=%s",
            session.session_id,
            persona_name,
        )
        if not spoken:
            await on_sentence(_LLM_FAIL_REPLY)
            return PromptTurnResult(reply_text=_LLM_FAIL_REPLY)
        # Partial reply already reached TTS: finish the turn with what was said.
    else:
        for sentence in splitter.flush():
            await _handle_sentence(sentence)

    # Marker-only reply: the consult started but nothing speakable was left.
    if consult_started and not spoken:
        await _speak(_HOLD_REPLY)
    if not spoken:
        await _speak(tenant_cfg.safe_fallback_reply)

    reply = " ".join(spoken)
    if transcript.strip():
        _append_history(state, "user", transcript.strip())
    _append_history(state, "assistant", reply)
    return PromptTurnResult(reply_text=reply, end_call=end_call, disposition=disposition)
