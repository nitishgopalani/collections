"""Prompt-mode agent — ASR text straight to the LLM, reply straight to TTS.

Second agent mode next to the flow engine (selected per tenant via
``TenantConfig.agent_mode == "prompt"``). No flow packs, no borrower store:
per-session conversation history is kept in-memory here, the tenant's persona
system prompt comes from config, and the reply is returned to the WS handler
which emits it through the SAME chunk/flow_class/done contract the flow engine
uses — the connector/go-server sees no difference.

Cross-leg consult hand-off (booking-confirm bot):

* The CUSTOMER persona signals a consult by ending a reply with a structured
  marker ``<consult booking_id=... hotel=... guest=...>``. We strip it and
  return the attrs as ``PromptTurnResult.consult_request``; the WS handler
  calls :func:`start_deferred_consult` (hold customer + dial the property)
  only after the go-server reports the hold announcement finished playing, so
  the caller hears the whole line before MOH starts.
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

from app.engine import turn_timing
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
_CONFERENCE_JOIN_MARKER_RE = re.compile(r"<conference_join\s*/?>", re.IGNORECASE)
_END_CALL_MARKER_RE = re.compile(r"<end_call\s*/?>", re.IGNORECASE)
_ATTR_RE = re.compile(r'(\w+)=(?:"([^"]*)"|([^\s>]+))')

# Public alias for the WS handler (deferred consult-start failure push).
CONSULT_FAIL_REPLY = _CONSULT_FAIL_REPLY
_CONFERENCE_JOIN_FAIL_REPLY = (
    "Maaf kijiye, third party ko abhi connect nahi kar paya."
)
CONFERENCE_JOIN_FAIL_REPLY = _CONFERENCE_JOIN_FAIL_REPLY


@dataclass
class PromptTurnResult:
    """Outcome of one prompt-mode turn, consumed by the WS handler."""

    reply_text: str
    end_call: bool = False
    disposition: str | None = None
    # <consult ...> marker attrs. The consult is NOT started inside the turn:
    # the WS handler starts it when the go-server reports the hold announcement
    # finished playing (playback_done), so the customer hears the whole line
    # before MOH starts and the property leg is dialled.
    consult_request: dict[str, str] | None = None
    # <conference_join> marker: deferred to playback_done like consult.
    conference_join_request: bool = False


@dataclass
class _PendingConsult:
    consult_id: str
    booking_id: str
    polls: int = 0
    interim_pushed: bool = False


@dataclass
class _PendingConferenceJoin:
    conference_id: str


@dataclass
class _SessionState:
    persona: str
    history: list[dict[str, str]] = field(default_factory=list)
    pending: _PendingConsult | None = None
    pending_conf: _PendingConferenceJoin | None = None
    property_turns: int = 0
    # Voicemail-detection window state (property leg, first 3 turns).
    # Weak phrases only fire on conjunction: 2+ weak hits across the window,
    # or any strong hit (which fires immediately on its own turn).
    vm_weak_count: int = 0


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
    keys = ("booking_id", "hotel", "guest", "checkin", "checkin_date", "borrower_phone", "phone")
    parts = [f"{k}={borrower_context[k]}" for k in keys if borrower_context.get(k)]
    if not parts:
        return ""
    return "BOOKING TO VERIFY: " + ", ".join(str(p) for p in parts)


async def start_deferred_consult(session: Any, attrs: dict[str, str]) -> bool:
    """Start the consult a turn requested, once its announcement has played.

    Called by the WS handler when the go-server reports playback_done for the
    turn that carried the <consult ...> marker (or its fallback timer fires) —
    NOT during the turn itself. This guarantees the customer hears the whole
    "please hold" line before the orchestrator starts MOH and dials the
    property. Returns False when consult_start failed (caller speaks the
    failure line); True when the consult is pending (or already was).
    """
    from app.clients.orchestrator import OrchestratorError

    state = _SESSIONS.get(session.session_id)
    if state is None:
        state = _SessionState(persona="")
        _SESSIONS[session.session_id] = state
    if state.pending is not None:
        return True
    try:
        state.pending = await _start_consult(session, attrs)
    except OrchestratorError:
        logger.exception(
            "prompt_agent deferred consult_start failed session_id=%s", session.session_id
        )
        return False
    logger.info(
        "prompt_agent consult started (post-playback) session_id=%s consult_id=%s booking_id=%s",
        session.session_id,
        state.pending.consult_id,
        state.pending.booking_id,
    )
    return True


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
        bc = dict(session.borrower_context or {})
        binding_ctx: dict[str, str] = {
            "tenant_id": str(session.tenant_id),
            "persona": "persona_property",
            "consult_id": str(out.get("consult_id", "")),
            "booking_id": attrs.get("booking_id", ""),
            "hotel": attrs.get("hotel", ""),
            "guest": attrs.get("guest", ""),
            "checkin": attrs.get("checkin", ""),
        }
        for key in ("borrower_phone", "phone", "guest_phone"):
            val = attrs.get(key) or bc.get(key)
            if val:
                binding_ctx[key] = str(val)
        consult_binding.register(consult_uuid, binding_ctx)
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


async def _poll_consult_status(consult_id: str) -> dict[str, Any]:
    """Return GET /v1/consult/{id} JSON, or {} on error."""
    from app.clients import orchestrator

    if not consult_id:
        return {}
    try:
        return await asyncio.to_thread(orchestrator.consult_status, consult_id=consult_id)
    except orchestrator.OrchestratorError:
        return {}


async def _try_voicemail_abort(
    session: Any, transcript: str, state: _SessionState
) -> PromptTurnResult | None:
    """Property leg only: hang up VM greeting and let orchestrator retry.

    Two-tier detection (see consult_voicemail.classify_transcript):
    * a STRONG phrase ("record your message", "रिकॉर्ड", "mailbox", beep...)
      fires immediately on its own turn;
    * a WEAK phrase ("not available", "busy") fires only on conjunction —
      either another weak/strong in the SAME transcript, or a second weak
      hit accumulated within the first 3 property turns.
    """
    from app.clients import orchestrator
    from app.engine import consult_binding
    from app.engine.consult_voicemail import classify_transcript

    if (session.agent_id or "").strip() != "persona_property":
        return None
    state.property_turns += 1
    if state.property_turns > 3:
        return None
    strong_hits, weak_hits = classify_transcript(transcript)
    fire = bool(strong_hits) or (state.vm_weak_count + len(weak_hits) >= 2)
    if not fire:
        # Accumulate weak hits for cross-turn conjunction within the window.
        state.vm_weak_count += len(weak_hits)
        return None
    ctx = consult_binding.lookup(session.session_id) or {}
    consult_id = str(ctx.get("consult_id") or "")
    booking_id = str(ctx.get("booking_id") or "")
    logger.info(
        "prompt_agent voicemail detected session_id=%s consult_id=%s booking_id=%s turn=%d",
        session.session_id,
        consult_id,
        booking_id,
        state.property_turns,
    )
    if consult_id:
        try:
            await asyncio.to_thread(orchestrator.consult_machine_answer, consult_id=consult_id)
        except orchestrator.OrchestratorError:
            logger.warning(
                "prompt_agent consult_machine_answer failed consult_id=%s", consult_id
            )
    return PromptTurnResult(
        reply_text="",
        end_call=True,
        disposition="VOICEMAIL_DETECTED",
    )


def derive_consult_push_budget_s(
    *,
    max_attempts: int | None = None,
    ring_budget_s: float | None = None,
    retry_gap_s: float | None = None,
) -> float:
    """Safety-net watcher budget: attempts*ring + gaps + 20s margin."""
    from app.config import get_settings

    settings = get_settings()
    attempts = max_attempts if max_attempts is not None else settings.consult_max_attempts
    ring = ring_budget_s if ring_budget_s is not None else settings.consult_ring_budget_s
    gap = retry_gap_s if retry_gap_s is not None else settings.consult_retry_gap_s
    return attempts * ring + max(0, attempts - 1) * gap + 20.0


def consult_attempts_remaining(status_out: dict[str, Any]) -> bool:
    """True while the orchestrator may still dial (non-terminal + attempts left)."""
    status = str(status_out.get("status", ""))
    if status not in ("originating", "ringing", "retrying"):
        return False
    attempt = int(status_out.get("attempt") or 0)
    max_attempts = int(status_out.get("max_attempts") or 0)
    return max_attempts > 0 and attempt < max_attempts


async def maybe_consult_interim_reply(session_id: str, tenant_cfg: Any) -> str | None:
    """Return the one-time interim hold line after dial attempt 1 fails."""
    from app.config import get_settings

    state = _SESSIONS.get(session_id)
    if state is None or state.pending is None or state.pending.interim_pushed:
        return None
    out = await _poll_consult_status(state.pending.consult_id)
    status = str(out.get("status", ""))
    attempt = int(out.get("attempt") or 0)
    if status != "retrying" or attempt < 2:
        return None
    state.pending.interim_pushed = True
    custom = str(getattr(tenant_cfg, "consult_retry_interim_reply", "") or "").strip()
    if custom:
        return custom
    return get_settings().consult_retry_interim_reply.strip()


async def consult_status_for_session(session_id: str) -> dict[str, Any]:
    """Poll orchestrator consult status for the session's pending consult."""
    state = _SESSIONS.get(session_id)
    if state is None or state.pending is None:
        return {}
    return await _poll_consult_status(state.pending.consult_id)


def pending_consult_id(session_id: str) -> str:
    """Return the orchestrator consult_id for a session's pending consult."""
    state = _SESSIONS.get(session_id)
    if state is None or state.pending is None:
        return ""
    return state.pending.consult_id


async def consult_hold_pause(consult_id: str) -> None:
    from app.clients import orchestrator

    if not consult_id:
        return
    try:
        await asyncio.to_thread(orchestrator.consult_hold_pause, consult_id=consult_id)
    except orchestrator.OrchestratorError:
        logger.warning("prompt_agent consult_hold_pause failed consult_id=%s", consult_id)


async def consult_hold_resume(consult_id: str) -> None:
    from app.clients import orchestrator

    if not consult_id:
        return
    try:
        await asyncio.to_thread(orchestrator.consult_hold_resume, consult_id=consult_id)
    except orchestrator.OrchestratorError:
        logger.warning("prompt_agent consult_hold_resume failed consult_id=%s", consult_id)


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
    status_out = await _poll_consult_status(pending.consult_id)
    if force_fail and consult_attempts_remaining(status_out):
        return None
    if force_fail or str(status_out.get("status", "")) == "failed":
        detail = str(status_out.get("detail", ""))
        note = detail if detail.startswith("no_answer_after_") else "could not reach the property"
        state.pending = None
        await _finish_consult(pending.consult_id, "failed")
        return {"confirmed": "unknown", "note": note, "detail": detail}
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


def _consult_no_answer_scripted_reply(tenant_cfg: Any, result: dict[str, str]) -> str:
    """Tenant fallback for retries-exhausted consult failure (Devanagari)."""
    from app.config import get_settings

    if result.get("confirmed", "unknown") != "unknown":
        return ""
    detail = str(result.get("detail", ""))
    if not detail.startswith("no_answer_after_"):
        return ""
    custom = str(getattr(tenant_cfg, "consult_no_answer_reply", "") or "").strip()
    if custom:
        return custom
    return get_settings().consult_no_answer_reply.strip()


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
    scripted = _consult_no_answer_scripted_reply(tenant_cfg, result)
    if scripted:
        _append_history(state, "assistant", scripted)
        return scripted
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
    reply = (raw_reply or "").strip()
    # Relay replies are pushed straight to TTS: strip any structured markers
    # the LLM may have (incorrectly) attached — they must never be spoken.
    reply = _CONSULT_MARKER_RE.sub("", reply)
    reply = _CONSULT_RESULT_MARKER_RE.sub("", reply)
    reply = _END_CALL_MARKER_RE.sub("", reply)
    reply = reply.strip() or fallback
    _append_history(state, "assistant", reply)
    return reply


def _conference_join_invite_number() -> str:
    return (os.getenv("CONFERENCE_THIRD_PARTY_NUMBER") or "9810319857").strip()


def _conference_join_caller_id() -> str:
    return (os.getenv("CONFERENCE_CALLER_ID") or "1725617003").strip()


def conference_join_connecting_reply(tenant_cfg: Any) -> str:
    custom = str(getattr(tenant_cfg, "conference_join_connecting_reply", "") or "").strip()
    if custom:
        return custom
    from app.config import get_settings

    return get_settings().conference_join_connecting_reply.strip()


def build_conference_join_announce(tenant_cfg: Any, outcome: str) -> str:
    """Scripted success/failure line after orchestrator reports terminal status."""
    from app.config import get_settings

    settings = get_settings()
    if outcome == "up":
        custom = str(getattr(tenant_cfg, "conference_join_success_reply", "") or "").strip()
        return custom or settings.conference_join_success_reply.strip()
    custom = str(getattr(tenant_cfg, "conference_join_fail_reply", "") or "").strip()
    return custom or settings.conference_join_fail_reply.strip()


def derive_conference_join_push_budget_s(*, ring_budget_s: float | None = None) -> float:
    """Safety-net watcher budget for a single CF1 join originate + ring."""
    from app.config import get_settings

    settings = get_settings()
    ring = ring_budget_s if ring_budget_s is not None else settings.conference_join_ring_budget_s
    return ring + 20.0


async def start_deferred_conference_join(session: Any) -> bool:
    """Start CF1 join after the connecting announcement played (playback_done)."""
    from app.clients.orchestrator import OrchestratorError

    state = _SESSIONS.get(session.session_id)
    if state is None:
        state = _SessionState(persona="")
        _SESSIONS[session.session_id] = state
    if state.pending_conf is not None:
        return True
    try:
        state.pending_conf = await _start_conference_join(session)
    except OrchestratorError:
        logger.exception(
            "prompt_agent deferred conference_join failed session_id=%s",
            session.session_id,
        )
        return False
    logger.info(
        "prompt_agent conference_join started (post-playback) session_id=%s conference_id=%s",
        session.session_id,
        state.pending_conf.conference_id,
    )
    return True


async def _start_conference_join(session: Any) -> _PendingConferenceJoin:
    from app.clients import orchestrator

    invite = _conference_join_invite_number()
    if not invite:
        raise orchestrator.OrchestratorError("no conference third-party number configured")
    out = await asyncio.to_thread(
        orchestrator.conference_join,
        session_uuid=str(session.session_id),
        invite_number=invite,
        caller_id=_conference_join_caller_id(),
    )
    conference_id = str(out.get("conference_id", ""))
    if not conference_id:
        raise orchestrator.OrchestratorError("conference_join returned no conference_id")
    return _PendingConferenceJoin(conference_id=conference_id)


async def _poll_conference_join_status(conference_id: str) -> dict[str, Any]:
    from app.clients import orchestrator

    if not conference_id:
        return {}
    try:
        return await asyncio.to_thread(
            orchestrator.conference_join_status, conference_id=conference_id
        )
    except orchestrator.OrchestratorError:
        return {}


async def conference_join_status_for_session(session_id: str) -> dict[str, Any]:
    state = _SESSIONS.get(session_id)
    if state is None or state.pending_conf is None:
        return {}
    return await _poll_conference_join_status(state.pending_conf.conference_id)


def has_pending_conference_join(session_id: str) -> bool:
    state = _SESSIONS.get(session_id)
    return state is not None and state.pending_conf is not None


async def _take_conference_join_outcome(
    state: _SessionState, *, force_fail: bool = False
) -> str | None:
    pending = state.pending_conf
    if pending is None:
        return None
    status_out = await _poll_conference_join_status(pending.conference_id)
    status = str(status_out.get("status", ""))
    if status == "up":
        state.pending_conf = None
        return "up"
    if status == "failed" or force_fail:
        state.pending_conf = None
        return "failed"
    if status in ("left", "finished"):
        state.pending_conf = None
        return "failed"
    if status in ("joining", "ringing", ""):
        return None
    return None


async def take_conference_join_outcome(
    session_id: str, *, force_fail: bool = False
) -> str | None:
    """Watcher/turn path: consume join outcome when decided, else None."""
    state = _SESSIONS.get(session_id)
    if state is None:
        return None
    return await _take_conference_join_outcome(state, force_fail=force_fail)


async def handle_prompt_turn(
    *,
    session: Any,
    transcript: str,
    llm: Any,
    tenant_cfg: Any,
    timing: Any | None = None,
) -> PromptTurnResult:
    """Run one prompt-mode turn: history + persona prompt -> LLM -> reply text."""
    if timing is not None:
        timing.set_path("buffered")
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

    vm_abort = await _try_voicemail_abort(session, transcript, state)
    if vm_abort is not None:
        if transcript.strip():
            _append_history(state, "user", transcript.strip())
        return vm_abort

    # Conference join in flight: never hallucinate success; brief hold only.
    if state.pending_conf is not None:
        outcome = await _take_conference_join_outcome(state)
        if outcome is None:
            if timing is not None:
                timing.set_path("hold")
            hold = conference_join_connecting_reply(tenant_cfg)
            if transcript.strip():
                _append_history(state, "user", transcript.strip())
            _append_history(state, "assistant", hold)
            return PromptTurnResult(reply_text=hold)
        announce = build_conference_join_announce(tenant_cfg, outcome)
        _append_history(
            state,
            "system",
            f"[CONFERENCE JOIN RESULT: status={outcome}]",
        )
        if transcript.strip():
            _append_history(state, "user", transcript.strip())
        _append_history(state, "assistant", announce)
        return PromptTurnResult(reply_text=announce)

    # Consult in flight (customer leg): inject the property outcome when it has
    # arrived, otherwise keep the caller on the line without an LLM round-trip.
    if state.pending is not None:
        result = await _resolve_pending_consult(state)
        if result is None:
            if timing is not None:
                timing.set_path("hold")
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
    if timing is not None:
        timing.mark(turn_timing.STAGE_LLM_START)
    try:
        raw_reply = await llm.complete(system_prompt, user_prompt, json_only=False)
    except Exception:
        logger.exception(
            "prompt_agent LLM call failed session_id=%s persona=%s",
            session.session_id,
            persona_name,
        )
        return PromptTurnResult(reply_text=_LLM_FAIL_REPLY)
    if timing is not None:
        # Buffered path: the whole reply lands at once.
        timing.mark(turn_timing.STAGE_LLM_FIRST_TOKEN)
        timing.mark(turn_timing.STAGE_LLM_DONE)
    reply = (raw_reply or "").strip()

    end_call = False
    disposition: str | None = None
    consult_request: dict[str, str] | None = None
    conference_join_request = False

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

    # CUSTOMER leg: the LLM asked for a property consult. Do NOT dial here —
    # hand the request to the WS handler, which starts it when the go-server
    # reports the hold announcement finished playing (playback_done), so the
    # customer hears the whole line before MOH starts.
    consult_match = _CONSULT_MARKER_RE.search(reply)
    if consult_match and not result_match:
        consult_request = _parse_marker_attrs(consult_match.group(1))
        reply = _CONSULT_MARKER_RE.sub("", reply).strip()
        if not reply:
            reply = _HOLD_REPLY
        logger.info(
            "prompt_agent consult requested (deferred to playback_done) "
            "session_id=%s booking_id=%s",
            session.session_id,
            consult_request.get("booking_id", ""),
        )

    # Conference moderator: dial third party after connecting line plays.
    if _CONFERENCE_JOIN_MARKER_RE.search(reply) and not result_match:
        conference_join_request = True
        reply = _CONFERENCE_JOIN_MARKER_RE.sub("", reply).strip()
        if not reply:
            reply = conference_join_connecting_reply(tenant_cfg)
        logger.info(
            "prompt_agent conference_join requested (deferred to playback_done) "
            "session_id=%s",
            session.session_id,
        )

    # Graceful goodbye: the LLM says the conversation is over.
    end_match = _END_CALL_MARKER_RE.search(reply)
    if end_match:
        reply = _END_CALL_MARKER_RE.sub("", reply).strip()
        end_call = True
        if disposition is None:
            disposition = "COMPLETED"

    if not reply:
        reply = tenant_cfg.safe_fallback_reply

    if transcript.strip():
        _append_history(state, "user", transcript.strip())
    _append_history(state, "assistant", reply)
    return PromptTurnResult(
        reply_text=reply,
        end_call=end_call,
        disposition=disposition,
        consult_request=consult_request,
        conference_join_request=conference_join_request,
    )


async def handle_prompt_turn_streaming(
    *,
    session: Any,
    transcript: str,
    llm: Any,
    tenant_cfg: Any,
    on_sentence: Callable[[str], Awaitable[None]],
    timing: Any | None = None,
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

    * An LLM error mid-stream ends the turn with what was already spoken;
      :data:`_LLM_FAIL_REPLY` is spoken only when nothing got out.
    """
    if timing is not None:
        timing.set_path("streaming")
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

    vm_abort = await _try_voicemail_abort(session, transcript, state)
    if vm_abort is not None:
        if transcript.strip():
            _append_history(state, "user", transcript.strip())
        return vm_abort

    # Conference join in flight: brief hold, no success announcement.
    if state.pending_conf is not None:
        outcome = await _take_conference_join_outcome(state)
        if outcome is None:
            if timing is not None:
                timing.set_path("hold")
            hold = conference_join_connecting_reply(tenant_cfg)
            if transcript.strip():
                _append_history(state, "user", transcript.strip())
            _append_history(state, "assistant", hold)
            await on_sentence(hold)
            return PromptTurnResult(reply_text=hold)
        announce = build_conference_join_announce(tenant_cfg, outcome)
        _append_history(state, "system", f"[CONFERENCE JOIN RESULT: status={outcome}]")
        if transcript.strip():
            _append_history(state, "user", transcript.strip())
        _append_history(state, "assistant", announce)
        await on_sentence(announce)
        return PromptTurnResult(reply_text=announce)

    # Consult in flight: same pre-LLM short-circuit as the non-streaming path.
    if state.pending is not None:
        result = await _resolve_pending_consult(state)
        if result is None:
            if timing is not None:
                timing.set_path("hold")
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
    consult_request: dict[str, str] | None = None
    conference_join_request = False

    async def _speak(text: str) -> None:
        if timing is not None and not spoken:
            timing.mark(turn_timing.STAGE_FIRST_SENTENCE)
        spoken.append(text)
        await on_sentence(text)

    async def _handle_sentence(sentence: str) -> None:
        """Existing marker parsing, applied to ONE completed sentence."""
        nonlocal end_call, disposition, consult_request, conference_join_request
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

        # CUSTOMER leg: the LLM asked for a property consult. Deferred to the
        # WS handler (playback_done) — see handle_prompt_turn for rationale.
        consult_match = _CONSULT_MARKER_RE.search(text)
        if consult_match and not result_match:
            consult_request = _parse_marker_attrs(consult_match.group(1))
            text = _CONSULT_MARKER_RE.sub("", text).strip()
            logger.info(
                "prompt_agent consult requested (deferred to playback_done) "
                "session_id=%s booking_id=%s",
                session.session_id,
                consult_request.get("booking_id", ""),
            )

        conf_match = _CONFERENCE_JOIN_MARKER_RE.search(text)
        if conf_match and not result_match:
            conference_join_request = True
            text = _CONFERENCE_JOIN_MARKER_RE.sub("", text).strip()
            logger.info(
                "prompt_agent conference_join requested (deferred to playback_done) "
                "session_id=%s",
                session.session_id,
            )

        # Graceful goodbye marker.
        end_match = _END_CALL_MARKER_RE.search(text)
        if end_match:
            text = _END_CALL_MARKER_RE.sub("", text).strip()
            end_call = True
            if disposition is None:
                disposition = "COMPLETED"

        if text:
            await _speak(text)

    try:
        if timing is not None:
            timing.mark(turn_timing.STAGE_LLM_START)
        stream = llm.stream(system_prompt, user_prompt)
        try:
            async for token in stream:
                if timing is not None:
                    timing.mark(turn_timing.STAGE_LLM_FIRST_TOKEN)
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
        if timing is not None:
            timing.mark(turn_timing.STAGE_LLM_DONE)

    # Marker-only reply: a consult or conference join was requested but nothing speakable.
    if consult_request is not None and not spoken:
        await _speak(_HOLD_REPLY)
    if conference_join_request and not spoken:
        await _speak(conference_join_connecting_reply(tenant_cfg))
    if not spoken:
        await _speak(tenant_cfg.safe_fallback_reply)

    reply = " ".join(spoken)
    if transcript.strip():
        _append_history(state, "user", transcript.strip())
    _append_history(state, "assistant", reply)
    return PromptTurnResult(
        reply_text=reply,
        end_call=end_call,
        disposition=disposition,
        consult_request=consult_request,
        conference_join_request=conference_join_request,
    )
