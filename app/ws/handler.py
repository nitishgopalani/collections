"""Brain WebSocket handler — EB-6 text contract (Go client ↔ collections engine)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.config import get_settings, tenant_config
from app.engine import consult_binding
from app.engine.prompt_agent import (
    CONFERENCE_JOIN_FAIL_REPLY,
    CONSULT_FAIL_REPLY,
    PromptTurnResult,
    append_push_assistant_history,
    build_conference_join_announce,
    build_consult_relay,
    consult_attempts_remaining,
    consult_status_for_session,
    consult_hold_pause,
    consult_hold_resume,
    derive_conference_join_push_budget_s,
    derive_consult_push_budget_s,
    handle_prompt_turn,
    handle_prompt_turn_streaming,
    has_pending_conference_join,
    has_pending_consult,
    maybe_consult_interim_reply,
    pending_consult_id,
    set_pending_conference_join_attrs,
    start_deferred_conference_join,
    start_deferred_consult,
    record_conference_join_push_history,
    take_conference_join_outcome,
    take_consult_result,
)
from app.engine.prompt_agent import clear_session as clear_prompt_session
from app.engine.turn import handle_turn
from app.engine.turn_timing import STAGE_FIRST_CHUNK_SENT, STAGE_TURN_DONE, PromptTurnTiming
from app.schemas.api import TurnRequest
from app.schemas.state import BorrowerRecord
from app.schemas.ws_contract import (
    CancelMessage,
    ChunkMessage,
    DoneMessage,
    ErrorMessage,
    FlowClassMessage,
    PlaybackDoneMessage,
    SessionEndMessage,
    SessionReadyMessage,
    SessionStartMessage,
    TurnMessage,
    parse_go_inbound,
)
from app.ws.conference_transcript import append_tap_turn, finalize_conference
from app.ws.borrower_context import normalize_borrower_context, parse_tap_only
from app.ws.borrower_resolve import resolve_asr_language, resolve_session_borrower
from app.ws import outbound_push
from app.ws.chunking import chunk_reply_for_tts
from app.ws.flow_class import flow_class_for_question_slot
from app.ws.routing import (
    resolve_agent_routing,
    resolve_session_defaults,
    resolve_session_tenant,
)
from app.ws.session import BrainWSSession
from app.ws.tenant_limits import SESSION_REGISTRY

logger = logging.getLogger(__name__)


# F3 (PREDUE-007 residual): resolve the PaisaLo per-scenario TTS voice from the
# hydrated borrower loan (dpd/npa) at session_start, so the go-server's
# DeadAirHandler speaks the dead-air apology in the SAME voice the call uses
# (predue/ondue→priya, postdue1/2→neha, postdue3→kabir, npa→amit). Mirrors the
# select_plo_scenario action's bucket logic (app/engine/actions.py) without
# depending on slots (which aren't hydrated until the opener runs).
_PLO_SCENARIO_VOICES = {
    "predue": "priya",
    "ondue": "priya",
    "postdue1": "neha",
    "postdue2": "neha",
    "postdue3": "kabir",
    "npa": "amit",
}


def _resolve_plo_scenario_voice(record, settings) -> str:
    """Return the PaisaLo scenario voice name for a borrower record.

    Honors TEST_PLO_SCENARIO override (goldens/lab); otherwise buckets on the
    loan's days_past_due / dpd / npa_flag. Returns "" if no loan fields exist.
    """
    loan = getattr(record, "loan", None) or {}
    override = (getattr(settings, "test_plo_scenario", "") or "").strip().lower()
    if override in _PLO_SCENARIO_VOICES:
        return _PLO_SCENARIO_VOICES[override]
    npa_raw = loan.get("npa_flag")
    npa = bool(npa_raw) and str(npa_raw).lower() not in {"0", "false", "no", ""}
    try:
        dpd = int(
            loan.get("days_past_due")
            if loan.get("days_past_due") is not None
            else (loan.get("dpd") or 0)
        )
    except (TypeError, ValueError):
        dpd = 0
    if npa:
        scenario = "npa"
    elif dpd < 0:
        scenario = "predue"
    elif dpd == 0:
        scenario = "ondue"
    elif dpd <= 30:
        scenario = "postdue1"
    elif dpd <= 60:
        scenario = "postdue2"
    else:
        scenario = "postdue3"
    return _PLO_SCENARIO_VOICES.get(scenario, "neha")


# Consult-result push (prompt mode): during hold the customer is silent (MOH),
# so no turns arrive to pick up the property leg's outcome. A per-session
# watcher polls for the result and pushes the relay as an unsolicited turn.
# Module-level so tests can shrink poll interval only; budget is derived from
# consult retry settings (attempts*ring + gaps + 20s margin).
CONSULT_PUSH_POLL_S = 2.0
CONFERENCE_JOIN_PUSH_POLL_S = 2.0

# Spoken when no response was heard after the final reprompt; the go-server
# hangs up noinput_hangup_delay_ms after this line finishes playing.
NOINPUT_DISCONNECT_LINE = (
    "Lagta hai aapki awaaz nahin aa rahi hai. Koi jawab na milne ke kaaran "
    "main yeh call disconnect kar raha hoon. Dhanyavaad."
)


def _normalize_test_session_start(payload: dict[str, Any], settings: Any) -> tuple[dict[str, Any], bool]:
    """Fill bare session_start fields when TEST_MODE is on; return (payload, was_bare)."""
    if not settings.test_mode or payload.get("type") != "session_start":
        return payload, False
    from app.engine.tenant_profile import get_tenant_profile

    normalized = dict(payload)
    was_bare = False
    if not str(normalized.get("session_id") or "").strip():
        normalized["session_id"] = str(uuid.uuid4())
        was_bare = True
    # DEBT-019: quarantine the test_tenant_id string-compare behind the profile's
    # test_borrower_id / test_agent_id fields. No profile → SOT defaults (back-compat).
    _test_profile = get_tenant_profile(
        (getattr(settings, "test_tenant_id", "") or "").strip()
    )
    _test_borrower_id = (
        _test_profile.test_borrower_id if _test_profile else "sot_test_borrower"
    )
    _test_agent_id = (
        _test_profile.test_agent_id if _test_profile else "salary-on-time-test"
    )
    if not str(normalized.get("borrower_id") or "").strip():
        normalized["borrower_id"] = _test_borrower_id
        was_bare = True
    if not str(normalized.get("agent_id") or "").strip():
        normalized["agent_id"] = _test_agent_id
        was_bare = True
    return normalized, was_bare


async def _send_model(ws: WebSocket, message: Any) -> None:
    if ws.client_state != WebSocketState.CONNECTED:
        return
    await ws.send_text(message.model_dump_json(exclude_none=True))


async def _persist_session_borrower(
    app_state: Any,
    session: BrainWSSession,
) -> BorrowerRecord | None:
    return await resolve_session_borrower(app_state.memory, session)


async def _consult_result_watcher(
    ws: WebSocket,
    app_state: Any,
    session: BrainWSSession,
    tenant_cfg: Any,
) -> None:
    """Poll for the consult outcome and push the relay as an unsolicited turn.

    Runs while a consult is pending. Every CONSULT_PUSH_POLL_S it checks
    CONSULT_RESULTS / consult status; when the outcome is decided (or the
    derived safety-net budget runs out -> forced failure, unless attempts
    remain) it emits the relay through the normal chunk/flow_class/done path.
    If a turn is mid-flight the watcher never consumes the result — it leaves
    it for that turn's own pending-consult check (or picks it up on the next
    tick once the turn is done), so unsolicited frames cannot interleave with
    a turn's reply frames.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + derive_consult_push_budget_s()
    while True:
        await asyncio.sleep(CONSULT_PUSH_POLL_S)
        if session.closed or ws.client_state != WebSocketState.CONNECTED:
            return
        if not has_pending_consult(session.session_id):
            # A caller turn already consumed and relayed the result.
            return
        if session.inflight_turn_id is not None:
            # Hand the result to the in-flight turn instead of pushing.
            continue
        interim = await maybe_consult_interim_reply(session.session_id, tenant_cfg)
        if interim:
            turn_id = f"consult-interim-{uuid.uuid4().hex[:8]}"
            logger.info(
                "brain ws consult retry interim push session_id=%s turn_id=%s",
                session.session_id,
                turn_id,
            )
            append_push_assistant_history(session.session_id, interim)
            await _push_consult_hold_announce(
                ws,
                session,
                turn_id,
                interim,
                disposition="CONSULT_RETRY_INTERIM",
            )
            continue
        force_fail = loop.time() >= deadline
        if force_fail:
            status_out = await consult_status_for_session(session.session_id)
            if consult_attempts_remaining(status_out):
                force_fail = False
        result = await take_consult_result(session.session_id, force_fail=force_fail)
        if result is None:
            continue
        reply = await build_consult_relay(
            session=session,
            llm=app_state.llm,
            tenant_cfg=tenant_cfg,
            result=result,
        )
        turn_id = f"consult-push-{uuid.uuid4().hex[:8]}"
        logger.info(
            "brain ws consult result push session_id=%s turn_id=%s confirmed=%s forced=%s",
            session.session_id,
            turn_id,
            result.get("confirmed", ""),
            force_fail,
        )
        async with session.send_lock:
            for seq, text in enumerate(chunk_reply_for_tts(reply)):
                await _send_model(ws, ChunkMessage(turn_id=turn_id, seq=seq, text=text))
            await _send_model(ws, FlowClassMessage(turn_id=turn_id, next="Default"))
            await _send_model(
                ws,
                DoneMessage(
                    turn_id=turn_id,
                    disposition="CONSULT_RELAYED",
                    end_call=False,
                    audit_id=None,
                ),
            )
        session.last_reply_text = reply
        return


async def _conference_join_watcher(
    ws: WebSocket,
    app_state: Any,
    session: BrainWSSession,
    tenant_cfg: Any,
) -> None:
    """Poll CF1 join status and push success/failure only on terminal states."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + derive_conference_join_push_budget_s()
    while True:
        await asyncio.sleep(CONFERENCE_JOIN_PUSH_POLL_S)
        if session.closed or ws.client_state != WebSocketState.CONNECTED:
            return
        if not has_pending_conference_join(session.session_id):
            return
        if session.inflight_turn_id is not None:
            continue
        force_fail = loop.time() >= deadline
        outcome = await take_conference_join_outcome(
            session.session_id, force_fail=force_fail
        )
        if outcome is None:
            continue
        reply = build_conference_join_announce(tenant_cfg, outcome)
        record_conference_join_push_history(session.session_id, outcome, reply)
        turn_id = f"conf-join-push-{uuid.uuid4().hex[:8]}"
        disposition = (
            "CONFERENCE_JOIN_UP" if outcome == "up" else "CONFERENCE_JOIN_FAILED"
        )
        logger.info(
            "brain ws conference join push session_id=%s turn_id=%s outcome=%s forced=%s",
            session.session_id,
            turn_id,
            outcome,
            force_fail,
        )
        async with session.send_lock:
            for seq, text in enumerate(chunk_reply_for_tts(reply)):
                await _send_model(ws, ChunkMessage(turn_id=turn_id, seq=seq, text=text))
            await _send_model(ws, FlowClassMessage(turn_id=turn_id, next="Default"))
            await _send_model(
                ws,
                DoneMessage(
                    turn_id=turn_id,
                    disposition=disposition,
                    end_call=False,
                    audit_id=None,
                ),
            )
        session.last_reply_text = reply
        return


def _ensure_conference_join_watcher(
    ws: WebSocket,
    app_state: Any,
    session: BrainWSSession,
    tenant_cfg: Any,
) -> None:
    task = session.conference_join_watch_task
    if task is not None and not task.done():
        return
    session.conference_join_watch_task = asyncio.create_task(
        _conference_join_watcher(ws, app_state, session, tenant_cfg)
    )


def _ensure_consult_watcher(
    ws: WebSocket,
    app_state: Any,
    session: BrainWSSession,
    tenant_cfg: Any,
) -> None:
    """Start the consult-result watcher for this session if none is running."""
    task = session.consult_watch_task
    if task is not None and not task.done():
        return
    session.consult_watch_task = asyncio.create_task(
        _consult_result_watcher(ws, app_state, session, tenant_cfg)
    )


async def _push_reply(
    ws: WebSocket,
    session: BrainWSSession,
    turn_id: str,
    text: str,
    *,
    disposition: str,
    end_call: bool = False,
    end_call_delay_ms: int = 0,
) -> None:
    """Emit one unsolicited chunk/flow_class/done unit under the send lock."""
    async with session.send_lock:
        for seq, chunk in enumerate(chunk_reply_for_tts(text)):
            await _send_model(ws, ChunkMessage(turn_id=turn_id, seq=seq, text=chunk))
        await _send_model(ws, FlowClassMessage(turn_id=turn_id, next="Default"))
        await _send_model(
            ws,
            DoneMessage(
                turn_id=turn_id,
                disposition=disposition,
                end_call=end_call,
                end_call_delay_ms=end_call_delay_ms,
                audit_id=None,
            ),
        )


async def _push_consult_hold_announce(
    ws: WebSocket,
    session: BrainWSSession,
    turn_id: str,
    text: str,
    *,
    disposition: str,
) -> None:
    """Push a line to the held customer: pause MOH, play TTS, resume on playback_done."""
    consult_id = pending_consult_id(session.session_id)
    if consult_id:
        await consult_hold_pause(consult_id)
        session.consult_hold_announce_turn_id = turn_id
    await _push_reply(ws, session, turn_id, text, disposition=disposition)
    if not consult_id:
        logger.warning(
            "brain ws consult hold announce without consult_id session_id=%s turn_id=%s",
            session.session_id,
            turn_id,
        )


# --- Deferred consult start (prompt mode) ------------------------------------
# A turn that carries a <consult ...> marker does NOT dial the property leg
# itself. The request is parked on the session; when the go-server reports the
# hold announcement finished playing (playback_done for that turn), the consult
# starts — so the customer hears the whole "please hold" line before the
# orchestrator pulls the AI leg and starts MOH. A fallback timer covers lost
# playback_done (e.g. barge-in cleared the playback).


def _register_deferred_consult(
    ws: WebSocket,
    app_state: Any,
    session: BrainWSSession,
    tenant_cfg: Any,
    turn_id: str,
    attrs: dict[str, str],
) -> None:
    session.pending_consult_request = dict(attrs)
    session.consult_request_turn_id = turn_id
    logger.info(
        "brain ws consult deferred until playback_done session_id=%s turn_id=%s",
        session.session_id,
        turn_id,
    )
    old = session.consult_fallback_task
    if old is not None and not old.done():
        old.cancel()

    async def _fallback() -> None:
        await asyncio.sleep(get_settings().consult_start_fallback_s)
        if session.closed or session.pending_consult_request is None:
            return
        logger.warning(
            "brain ws consult playback_done never arrived; starting via fallback "
            "session_id=%s turn_id=%s",
            session.session_id,
            turn_id,
        )
        _launch_consult_start(ws, app_state, session, tenant_cfg)

    session.consult_fallback_task = asyncio.create_task(_fallback())


def _launch_consult_start(
    ws: WebSocket,
    app_state: Any,
    session: BrainWSSession,
    tenant_cfg: Any,
) -> None:
    """Fire the parked consult request now (idempotent)."""
    attrs = session.pending_consult_request
    if attrs is None:
        return
    session.pending_consult_request = None
    session.consult_request_turn_id = None
    fallback = session.consult_fallback_task
    session.consult_fallback_task = None
    if fallback is not None and fallback is not asyncio.current_task():
        fallback.cancel()
    session.consult_start_task = asyncio.create_task(
        _start_deferred_consult_now(ws, app_state, session, tenant_cfg, attrs)
    )


async def _start_deferred_consult_now(
    ws: WebSocket,
    app_state: Any,
    session: BrainWSSession,
    tenant_cfg: Any,
    attrs: dict[str, str],
) -> None:
    ok = await start_deferred_consult(session, attrs)
    if session.closed or ws.client_state != WebSocketState.CONNECTED:
        return
    if ok:
        _ensure_consult_watcher(ws, app_state, session, tenant_cfg)
        return
    # consult_start failed: tell the waiting customer instead of dead air.
    append_push_assistant_history(session.session_id, CONSULT_FAIL_REPLY)
    await _push_reply(
        ws,
        session,
        f"consult-fail-{uuid.uuid4().hex[:8]}",
        CONSULT_FAIL_REPLY,
        disposition="CONSULT_FAILED",
    )


# --- Deferred conference join (CF1.5 conference moderator) --------------------


def _register_deferred_conference_join(
    ws: WebSocket,
    app_state: Any,
    session: BrainWSSession,
    tenant_cfg: Any,
    turn_id: str,
    attrs: dict[str, str],
) -> None:
    session.pending_conference_join_request = dict(attrs)
    session.conference_join_request_turn_id = turn_id
    set_pending_conference_join_attrs(session.session_id, attrs)
    logger.info(
        "brain ws conference_join deferred until playback_done session_id=%s turn_id=%s",
        session.session_id,
        turn_id,
    )
    old = session.conference_join_fallback_task
    if old is not None and not old.done():
        old.cancel()

    async def _fallback() -> None:
        await asyncio.sleep(get_settings().consult_start_fallback_s)
        if session.closed or session.pending_conference_join_request is None:
            return
        logger.warning(
            "brain ws conference_join playback_done never arrived; starting via fallback "
            "session_id=%s turn_id=%s",
            session.session_id,
            turn_id,
        )
        _launch_conference_join_start(ws, app_state, session, tenant_cfg)

    session.conference_join_fallback_task = asyncio.create_task(_fallback())


def _launch_conference_join_start(
    ws: WebSocket,
    app_state: Any,
    session: BrainWSSession,
    tenant_cfg: Any,
) -> None:
    if session.pending_conference_join_request is None:
        return
    session.pending_conference_join_request = None
    session.conference_join_request_turn_id = None
    fallback = session.conference_join_fallback_task
    session.conference_join_fallback_task = None
    if fallback is not None and fallback is not asyncio.current_task():
        fallback.cancel()
    session.conference_join_start_task = asyncio.create_task(
        _start_deferred_conference_join_now(ws, app_state, session, tenant_cfg)
    )


async def _start_deferred_conference_join_now(
    ws: WebSocket,
    app_state: Any,
    session: BrainWSSession,
    tenant_cfg: Any,
) -> None:
    ok = await start_deferred_conference_join(session)
    if session.closed or ws.client_state != WebSocketState.CONNECTED:
        return
    if ok:
        _ensure_conference_join_watcher(ws, app_state, session, tenant_cfg)
        return
    append_push_assistant_history(session.session_id, CONFERENCE_JOIN_FAIL_REPLY)
    await _push_reply(
        ws,
        session,
        f"conf-join-fail-{uuid.uuid4().hex[:8]}",
        CONFERENCE_JOIN_FAIL_REPLY,
        disposition="CONFERENCE_JOIN_FAILED",
    )


# --- No-input reprompts (prompt mode) -----------------------------------------
# Armed when a reply finishes PLAYING (playback_done) and cancelled by the next
# caller turn. Fires after noinput_reprompt_s of silence: repeats the last
# question, up to noinput_max_reprompts times; then announces the disconnect
# and ends the call noinput_hangup_delay_ms after that line finishes playing.


def _cancel_noinput_timer(session: BrainWSSession) -> None:
    task = session.noinput_task
    session.noinput_task = None
    if task is not None and not task.done():
        task.cancel()


def _arm_noinput_timer(
    ws: WebSocket,
    app_state: Any,
    session: BrainWSSession,
    tenant_cfg: Any,
) -> None:
    if getattr(tenant_cfg, "agent_mode", "") != "prompt" or session.closed:
        return
    # On hold (consult pending or parked) the customer's silence is expected.
    if session.pending_consult_request is not None or has_pending_consult(session.session_id):
        return
    if session.pending_conference_join_request is not None or has_pending_conference_join(session.session_id):
        return
    _cancel_noinput_timer(session)
    session.noinput_task = asyncio.create_task(
        _noinput_watch(ws, app_state, session, tenant_cfg)
    )


async def _noinput_watch(
    ws: WebSocket,
    app_state: Any,
    session: BrainWSSession,
    tenant_cfg: Any,
) -> None:
    settings = get_settings()
    await asyncio.sleep(settings.noinput_reprompt_s)
    if session.closed or ws.client_state != WebSocketState.CONNECTED:
        return
    if session.inflight_turn_id is not None:
        return
    if session.pending_consult_request is not None or has_pending_consult(session.session_id):
        return
    if session.pending_conference_join_request is not None or has_pending_conference_join(session.session_id):
        return
    session.noinput_count += 1
    if session.noinput_count <= settings.noinput_max_reprompts:
        text = session.last_reply_text or tenant_cfg.safe_fallback_reply
        turn_id = f"noinput-{session.noinput_count}-{uuid.uuid4().hex[:6]}"
        logger.info(
            "brain ws no-input reprompt session_id=%s attempt=%d turn_id=%s",
            session.session_id,
            session.noinput_count,
            turn_id,
        )
        append_push_assistant_history(session.session_id, text)
        # playback_done for this push re-arms the timer for the next window.
        await _push_reply(ws, session, turn_id, text, disposition="NOINPUT_REPROMPT")
        return
    turn_id = f"noinput-end-{uuid.uuid4().hex[:6]}"
    logger.info(
        "brain ws no-input disconnect session_id=%s turn_id=%s",
        session.session_id,
        turn_id,
    )
    await _push_reply(
        ws,
        session,
        turn_id,
        NOINPUT_DISCONNECT_LINE,
        disposition="NOINPUT_DISCONNECT",
        end_call=True,
        end_call_delay_ms=settings.noinput_hangup_delay_ms,
    )


async def _run_prompt_turn_streaming(
    ws: WebSocket,
    app_state: Any,
    session: BrainWSSession,
    msg: TurnMessage,
    tenant_cfg: Any,
    *,
    deadline_s: float,
    fallback_text: str,
    timing: PromptTurnTiming | None = None,
) -> None:
    """Streaming prompt-mode turn: sentences become chunk frames as the LLM generates.

    Same chunk/flow_class/done contract as the non-streaming path — the
    go-server sees no difference — but the first chunk goes out as soon as the
    first sentence completes, instead of after the whole LLM reply. done is
    sent when the LLM stream ends. Cancel/supersede cancels the inflight task,
    which aborts (closes) the LLM stream via handle_prompt_turn_streaming.

    The session send lock is held for the WHOLE turn (chunks stream over
    seconds while the LLM generates), so the consult-push watcher can never
    interleave its unsolicited frames with this turn's frames. If the turn
    started (or left) a pending consult, the watcher is (re)armed on every
    exit path so a silent caller still hears the outcome.
    """
    cancel_event = session.register_turn(msg.turn_id)
    seq = 0

    async def _emit_sentence(text: str) -> None:
        nonlocal seq
        if cancel_event.is_set() or session.is_cancelled(msg.turn_id):
            raise asyncio.CancelledError("turn cancelled")
        await _send_model(ws, ChunkMessage(turn_id=msg.turn_id, seq=seq, text=text))
        if timing is not None:
            timing.mark(STAGE_FIRST_CHUNK_SENT)
        seq += 1

    async def _execute() -> PromptTurnResult:
        if cancel_event.is_set() or session.is_cancelled(msg.turn_id):
            raise asyncio.CancelledError("turn cancelled")
        return await handle_prompt_turn_streaming(
            session=session,
            transcript=msg.transcript,
            llm=app_state.llm,
            tenant_cfg=tenant_cfg,
            on_sentence=_emit_sentence,
            timing=timing,
        )

    try:
        async with session.send_lock:
            task = asyncio.create_task(_execute())
            session.inflight_task = task
            try:
                result = await asyncio.wait_for(task, timeout=deadline_s)
            except TimeoutError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                logger.warning(
                    "brain ws prompt streaming turn deadline exceeded "
                    "session_id=%s tenant_id=%s turn_id=%s",
                    session.session_id,
                    session.tenant_id,
                    msg.turn_id,
                )
                await _send_model(
                    ws, ErrorMessage(turn_id=msg.turn_id, fallback_text=fallback_text)
                )
                return
            except asyncio.CancelledError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                return
            finally:
                session.clear_turn(msg.turn_id)

            if cancel_event.is_set() or session.is_cancelled(msg.turn_id):
                return

            # Sentences were already emitted during the stream; close out the turn.
            await _send_model(ws, FlowClassMessage(turn_id=msg.turn_id, next="Default"))
            await _send_model(
                ws,
                DoneMessage(
                    turn_id=msg.turn_id,
                    disposition=result.disposition,
                    end_call=result.end_call,
                    end_call_delay_ms=(
                        get_settings().end_call_grace_ms if result.end_call else 0
                    ),
                    audit_id=None,
                ),
            )
        session.last_reply_text = result.reply_text
        if result.consult_request is not None:
            _register_deferred_consult(
                ws, app_state, session, tenant_cfg, msg.turn_id, result.consult_request
            )
        if result.conference_join_request is not None:
            _register_deferred_conference_join(
                ws, app_state, session, tenant_cfg, msg.turn_id, result.conference_join_request
            )
    finally:
        if timing is not None:
            timing.mark(STAGE_TURN_DONE)
            logger.info(timing.log_line())
        if has_pending_consult(session.session_id):
            _ensure_consult_watcher(ws, app_state, session, tenant_cfg)
        if has_pending_conference_join(session.session_id):
            _ensure_conference_join_watcher(ws, app_state, session, tenant_cfg)


async def _run_prompt_turn(
    ws: WebSocket,
    app_state: Any,
    session: BrainWSSession,
    msg: TurnMessage,
    tenant_cfg: Any,
    *,
    deadline_s: float,
    fallback_text: str,
) -> None:
    """Prompt-mode turn: LLM reply through the same chunk/flow_class/done contract."""
    timing = PromptTurnTiming(str(session.session_id), msg.turn_id)
    # Feature flag: streaming only for tenants that opt in (booking-confirm)
    # AND an LLM client that actually implements stream() — anything else
    # (Groq, scripted test doubles) keeps the buffered path.
    if bool(getattr(tenant_cfg, "streaming_llm", False)) and callable(
        getattr(app_state.llm, "stream", None)
    ):
        await _run_prompt_turn_streaming(
            ws,
            app_state,
            session,
            msg,
            tenant_cfg,
            deadline_s=deadline_s,
            fallback_text=fallback_text,
            timing=timing,
        )
        return

    cancel_event = session.register_turn(msg.turn_id)

    async def _execute() -> PromptTurnResult:
        if cancel_event.is_set() or session.is_cancelled(msg.turn_id):
            raise asyncio.CancelledError("turn cancelled")
        return await handle_prompt_turn(
            session=session,
            transcript=msg.transcript,
            llm=app_state.llm,
            tenant_cfg=tenant_cfg,
            timing=timing,
        )

    task = asyncio.create_task(_execute())
    session.inflight_task = task
    try:
        result = await asyncio.wait_for(task, timeout=deadline_s)
    except TimeoutError:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logger.warning(
            "brain ws prompt turn deadline exceeded session_id=%s tenant_id=%s turn_id=%s",
            session.session_id,
            session.tenant_id,
            msg.turn_id,
        )
        await _send_model(ws, ErrorMessage(turn_id=msg.turn_id, fallback_text=fallback_text))
        return
    except asyncio.CancelledError:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return
    finally:
        session.clear_turn(msg.turn_id)

    # The turn may have started a consult; make sure the result watcher runs so
    # the outcome is pushed even if the customer stays silent on hold.
    if has_pending_consult(session.session_id):
        _ensure_consult_watcher(ws, app_state, session, tenant_cfg)
    if has_pending_conference_join(session.session_id):
        _ensure_conference_join_watcher(ws, app_state, session, tenant_cfg)

    if cancel_event.is_set() or session.is_cancelled(msg.turn_id):
        return

    async with session.send_lock:
        for seq, text in enumerate(chunk_reply_for_tts(result.reply_text)):
            if cancel_event.is_set() or session.is_cancelled(msg.turn_id):
                return
            await _send_model(ws, ChunkMessage(turn_id=msg.turn_id, seq=seq, text=text))
            timing.mark(STAGE_FIRST_CHUNK_SENT)
        await _send_model(ws, FlowClassMessage(turn_id=msg.turn_id, next="Default"))
        await _send_model(
            ws,
            DoneMessage(
                turn_id=msg.turn_id,
                disposition=result.disposition,
                end_call=result.end_call,
                end_call_delay_ms=(
                    get_settings().end_call_grace_ms if result.end_call else 0
                ),
                audit_id=None,
            ),
        )
    session.last_reply_text = result.reply_text
    if result.consult_request is not None:
        _register_deferred_consult(
            ws, app_state, session, tenant_cfg, msg.turn_id, result.consult_request
        )
    if result.conference_join_request is not None:
        _register_deferred_conference_join(
            ws, app_state, session, tenant_cfg, msg.turn_id, result.conference_join_request
        )
    timing.mark(STAGE_TURN_DONE)
    logger.info(timing.log_line())


async def _run_tap_only_turn(
    ws: WebSocket,
    session: BrainWSSession,
    msg: TurnMessage,
) -> None:
    """CF2.2 transcript-only tap: ASR input arrives as turns; never LLM/TTS/actions."""
    cancel_event = session.register_turn(msg.turn_id)
    try:
        transcript = msg.transcript.strip()
        logger.info(
            "brain ws tap_only transcript session_id=%s speaker_label=%s "
            "parent_session_uuid=%s turn_id=%s transcript=%s",
            session.session_id,
            session.speaker_label,
            session.parent_session_uuid,
            msg.turn_id,
            transcript,
        )
        if transcript and session.parent_session_uuid and session.speaker_label:
            append_tap_turn(
                parent_session_uuid=session.parent_session_uuid,
                speaker_label=session.speaker_label,
                text=transcript,
                turn_id=msg.turn_id,
            )
        if cancel_event.is_set() or session.is_cancelled(msg.turn_id):
            return
        await _send_model(ws, FlowClassMessage(turn_id=msg.turn_id, next="Default"))
        await _send_model(
            ws,
            DoneMessage(
                turn_id=msg.turn_id,
                disposition="tap_only",
                end_call=False,
                end_call_delay_ms=0,
                audit_id=None,
            ),
        )
    finally:
        session.clear_turn(msg.turn_id)


def _extract_failure_url(exc: BaseException) -> str:
    """Walk an exception chain to find the URL/hostname that caused a network failure.

    HARDEN-1 F1: the bare ``[Errno -2] Name or service not known`` log line names
    no host, so the operator can't tell which dependency died. httpx attaches the
    request URL to its transport errors; socket.gaierror does not, but the URL is
    on the parent httpx exception. Walk ``__cause__``/``__context__``/``__suppress_context__``
    and return the first URL/host found, else "".
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        for attr in ("request", "_request"):
            req = getattr(cur, attr, None)
            if req is not None:
                url = getattr(req, "url", None)
                if url is not None:
                    return str(url)
        # httpx.ConnectError sometimes carries the URL in its args/message.
        msg = getattr(cur, "args", None)
        if msg:
            for a in msg:
                if isinstance(a, str) and "://" in a:
                    return a
        cur = cur.__cause__ or cur.__context__
    return ""


def _render_opener_fallback(
    app_state: Any,
    session: BrainWSSession,
    tenant_cfg: Any,
) -> str:
    """Render a deterministic opener greeting via NLG (no LLM/KB dependency).

    HARDEN-1 F1: when the opener turn crashes (e.g. transient DNS on persist/audit),
    the caller must still hear a greeting instead of dead air. We render the
    tenant's ``opener_fallback_reply_id`` template from the reply manifest — pure
    string interpolation, no network calls. If the template is unconfigured or
    can't render (missing slot, unknown id), fall back to the tenant's static
    ``safe_fallback_reply`` so the caller always hears something.
    """
    reply_id = (getattr(tenant_cfg, "opener_fallback_reply_id", "") or "").strip()
    if not reply_id:
        return tenant_cfg.safe_fallback_reply
    try:
        from app.engine.nlg import render_resolved
        from app.engine.tracker import new_conversation_state

        state = new_conversation_state(
            session.session_id,
            session.tenant_id,
            session.borrower_id,
        )
        # Hydrate customer_name from the session borrower_context so SOT's
        # sot_greeting (which interpolates {customer_name}) renders cleanly.
        borrower_name = str(session.borrower_context.get("borrower_name", "") or "")
        if borrower_name:
            state.slots["customer_name"] = borrower_name
        flows = app_state.flows
        resolved = render_resolved(
            reply_id,
            state,
            flows,
            locale=session.locale,
            channel="voice",
        )
        text = (resolved.text or "").strip()
        return text or tenant_cfg.safe_fallback_reply
    except Exception:  # noqa: BLE001 — opener fallback must never raise
        logger.warning(
            "brain ws opener_fallback render failed session_id=%s reply_id=%s; "
            "using safe_fallback_reply",
            session.session_id,
            reply_id,
            exc_info=True,
        )
        return tenant_cfg.safe_fallback_reply


async def _run_turn(
    ws: WebSocket,
    app_state: Any,
    session: BrainWSSession,
    msg: TurnMessage,
    *,
    deadline_s: float,
    fallback_text: str,
) -> None:
    if session.tap_only:
        await _run_tap_only_turn(ws, session, msg)
        return

    # Prompt-mode tenants bypass the flow engine entirely (booking-confirm bot).
    tenant_cfg = tenant_config(session.tenant_id)
    if tenant_cfg.agent_mode == "prompt":
        await _run_prompt_turn(
            ws,
            app_state,
            session,
            msg,
            tenant_cfg,
            deadline_s=deadline_s,
            fallback_text=fallback_text,
        )
        return

    cancel_event = session.register_turn(msg.turn_id)
    tenant_id = session.tenant_id

    turn_meta: dict[str, Any] = {}
    if not msg.transcript.strip():
        turn_meta["opener"] = True
    if session.force_flow:
        turn_meta["force_flow"] = session.force_flow
    if session.borrower_context:
        turn_meta["borrower_context"] = dict(session.borrower_context)

    request = TurnRequest(
        call_id=session.session_id,
        tenant_id=tenant_id,
        borrower_id=session.borrower_id,
        agent_id=session.agent_id,
        pack_id=session.pack_id or None,
        locale=session.locale,
        transcript=msg.transcript,
        turn_meta=turn_meta,
    )

    async def _emit_gated_chunks(
        reply_text: str,
        *,
        voice_id: str | None = None,
        tts_model: str | None = None,
        tts_pace: float | None = None,
    ) -> None:
        """Stream gated reply chunks to Go before persist completes (gate already passed)."""
        for seq, text in enumerate(chunk_reply_for_tts(reply_text)):
            if cancel_event.is_set() or session.is_cancelled(msg.turn_id):
                return
            await _send_model(
                ws,
                ChunkMessage(
                    turn_id=msg.turn_id,
                    seq=seq,
                    text=text,
                    voice_id=voice_id,
                    tts_model=tts_model,
                    tts_pace=tts_pace,
                ),
            )

    async def _execute() -> Any:
        if cancel_event.is_set() or session.is_cancelled(msg.turn_id):
            raise asyncio.CancelledError("turn cancelled")
        return await handle_turn(
            request,
            memory=app_state.memory,
            kb=app_state.kb,
            llm=app_state.llm,
            tools=app_state.tools,
            flows=app_state.flows,
            overrides=app_state.overrides,
            on_gated_reply=_emit_gated_chunks,
        )

    task = asyncio.create_task(_execute())
    session.inflight_task = task
    try:
        response = await asyncio.wait_for(task, timeout=deadline_s)
    except TimeoutError:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logger.warning(
            "brain ws turn deadline exceeded session_id=%s tenant_id=%s turn_id=%s",
            session.session_id,
            session.tenant_id,
            msg.turn_id,
        )
        await _send_model(
            ws,
            ErrorMessage(turn_id=msg.turn_id, fallback_text=fallback_text),
        )
        return
    except asyncio.CancelledError:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logger.info(
            "brain ws turn cancelled session_id=%s tenant_id=%s turn_id=%s",
            session.session_id,
            session.tenant_id,
            msg.turn_id,
        )
        return
    except Exception as exc:
        # HARDEN-1 F1: an unhandled exception in handle_turn (e.g. transient DNS
        # on persist/audit → "[Errno -2] Name or service not known") used to kill
        # the turn silently — the caller heard dead air. Now we log the failing
        # URL/host and, for the opener turn, emit a deterministic template
        # greeting via NLG (no LLM/KB) so the caller still hears a greeting.
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        fail_url = _extract_failure_url(exc)
        is_opener = not (msg.transcript or "").strip()
        logger.warning(
            "brain ws turn crashed session_id=%s tenant_id=%s turn_id=%s opener=%s "
            "exc_type=%s exc=%s fail_url=%s",
            session.session_id,
            session.tenant_id,
            msg.turn_id,
            is_opener,
            type(exc).__name__,
            exc,
            fail_url,
        )
        if is_opener:
            tenant_cfg = tenant_config(session.tenant_id)
            greeting = _render_opener_fallback(app_state, session, tenant_cfg)
            logger.info(
                "brain ws opener_fallback session_id=%s tenant_id=%s turn_id=%s "
                "reply_id=%s text_len=%d",
                session.session_id,
                session.tenant_id,
                msg.turn_id,
                tenant_cfg.opener_fallback_reply_id or "",
                len(greeting),
            )
            for seq, text in enumerate(chunk_reply_for_tts(greeting)):
                if cancel_event.is_set() or session.is_cancelled(msg.turn_id):
                    return
                await _send_model(
                    ws,
                    ChunkMessage(turn_id=msg.turn_id, seq=seq, text=text),
                )
            # The opener asks for identity confirmation (a yes/no slot), so
            # endpointing on the Go side should treat the next caller input as
            # a short acknowledgement, not free-form speech.
            await _send_model(
                ws,
                FlowClassMessage(
                    turn_id=msg.turn_id,
                    next=flow_class_for_question_slot("identity_response"),
                ),
            )
            await _send_model(
                ws,
                DoneMessage(
                    turn_id=msg.turn_id,
                    disposition="OPENER_FALLBACK",
                    end_call=False,
                    audit_id=None,
                ),
            )
            return
        await _send_model(
            ws,
            ErrorMessage(turn_id=msg.turn_id, fallback_text=fallback_text),
        )
        return
    finally:
        session.clear_turn(msg.turn_id)

    if cancel_event.is_set() or session.is_cancelled(msg.turn_id):
        return

    state = await app_state.memory.load_state(session.session_id)
    question_slot = None
    if state is not None:
        raw = state.slots.get("last_question_slot")
        if isinstance(raw, str):
            question_slot = raw

    await _send_model(
        ws,
        FlowClassMessage(
            turn_id=msg.turn_id,
            next=flow_class_for_question_slot(question_slot),
        ),
    )
    await _send_model(
        ws,
        DoneMessage(
            turn_id=msg.turn_id,
            disposition=response.disposition,
            end_call=response.end_call,
            audit_id=response.audit_id or None,
        ),
    )


async def handle_brain_websocket(ws: WebSocket) -> None:
    """Accept one persistent EB-6 session; text in/out only (no audio)."""
    await ws.accept()
    settings = get_settings()
    # Pre-session window: no tenant is known until session_start arrives, so use
    # the default tenant's safe fallback. It is re-resolved to the real tenant
    # once session_start is parsed (Phase C).
    fallback_text = tenant_config(settings.default_tenant_id).safe_fallback_reply
    deadline_s = max(settings.ws_turn_deadline_ms, 100) / 1000.0

    session: BrainWSSession | None = None
    acquired_tenant: str | None = None

    try:
        while True:
            raw = await ws.receive_text()
            try:
                payload = json.loads(raw)
                test_bare_session = False
                if settings.test_mode:
                    payload, test_bare_session = _normalize_test_session_start(payload, settings)
                inbound = parse_go_inbound(payload)
            except (json.JSONDecodeError, ValueError) as exc:
                logger.warning("brain ws invalid message: %s", exc)
                continue

            if isinstance(inbound, SessionStartMessage):
                borrower_context = normalize_borrower_context(inbound.borrower_context)
                logger.info(
                    "brain ws session_start received session_id=%s borrower_id=%s "
                    "ctx_phone=%s ctx_name=%s ctx_keys=%s",
                    inbound.session_id,
                    inbound.borrower_id,
                    borrower_context.get("phone", ""),
                    borrower_context.get("borrower_name", ""),
                    sorted(borrower_context.keys()),
                )
                force_flow, routed_tenant = resolve_agent_routing(inbound.agent_id)
                # HARDEN-1: opener from client_id when agent_id omitted (live ARI
                # sessions carry client_id=paisalo|salary-on-time, not *-test agents).
                if force_flow is None and (inbound.client_id or "").strip():
                    force_flow, _ = resolve_agent_routing(inbound.client_id.strip())

                test_force = (getattr(settings, "test_force_tenant", "") or "").strip()
                if test_force:
                    # Explicit override only (empty default). Not TEST_MODE/TEST_TENANT_ID.
                    # DEBT-019: resolve the test agent via the forced tenant's profile
                    # (test_agent_id), falling back to the SOT test agent.
                    from app.engine.tenant_profile import get_tenant_profile

                    tenant_id, tenant_source = test_force, "test_force_tenant"
                    _tf_profile = get_tenant_profile(test_force)
                    test_agent = (
                        _tf_profile.test_agent_id if _tf_profile else "salary-on-time-test"
                    )
                    forced, _ = resolve_agent_routing(test_agent)
                    if forced:
                        force_flow = forced
                elif test_bare_session:
                    # Bare TEST_MODE session_start (no client_id): lab convenience only.
                    from app.engine.tenant_profile import get_tenant_profile

                    tenant_id, tenant_source = settings.test_tenant_id, "test_bare"
                    if force_flow is None:
                        _tb_profile = get_tenant_profile(
                            (settings.test_tenant_id or "").strip()
                        )
                        test_agent = (
                            _tb_profile.test_agent_id
                            if _tb_profile
                            else "salary-on-time-test"
                        )
                        forced, _ = resolve_agent_routing(test_agent)
                        if forced:
                            force_flow = forced
                else:
                    # Production + UAT truth path: tenant from session_start client_id.
                    # TEST_MODE must NOT pin tenant (G-A2-01).
                    tenant_id, tenant_source = resolve_session_tenant(
                        client_id=inbound.client_id,
                        routed_tenant=routed_tenant,
                        inbound_tenant_id=inbound.tenant_id,
                        default_tenant_id=settings.default_tenant_id,
                    )
                # Property-leg persona binding: a consult that the customer
                # persona started pre-registered its booking context under the
                # consult AI leg's uuid; if THIS session_id matches, this is
                # that leg — run it as persona_property for the registered
                # tenant with the booking context injected. Beats every other
                # tenant/persona source (including TEST_FORCE_TENANT).
                consult_ctx = consult_binding.lookup(inbound.session_id)
                if consult_ctx is not None:
                    tenant_id = str(consult_ctx.get("tenant_id") or tenant_id)
                    tenant_source = "consult_binding"
                    force_flow = None
                    for key in ("booking_id", "hotel", "guest", "checkin", "borrower_phone", "phone", "guest_phone"):
                        if consult_ctx.get(key):
                            borrower_context.setdefault(key, str(consult_ctx[key]))
                logger.info(
                    "brain ws tenant resolved session_id=%s tenant_id=%s source=%s "
                    "client_id=%s session_tenant_id=%s",
                    inbound.session_id,
                    tenant_id,
                    tenant_source,
                    inbound.client_id or "",
                    inbound.tenant_id or "",
                )

                # Tenant-specific config now that the tenant is known.
                tenant_cfg = tenant_config(tenant_id)
                fallback_text = tenant_cfg.safe_fallback_reply

                # Per-tenant concurrency cap (Phase C). Acquire once per connection.
                if acquired_tenant is None:
                    cap = tenant_cfg.max_concurrent_sessions
                    if not SESSION_REGISTRY.try_acquire(tenant_id, cap):
                        logger.warning(
                            "brain ws session rejected: tenant_id=%s at concurrency cap=%d "
                            "(active=%d) session_id=%s",
                            tenant_id,
                            cap,
                            SESSION_REGISTRY.active(tenant_id),
                            inbound.session_id,
                        )
                        await ws.close(code=1013)  # 1013 = try again later
                        return
                    acquired_tenant = tenant_id

                # Fill pack/agent/locale from tenant defaults when the caller omitted
                # them; explicit session_start values always win. locale "omitted" is
                # detected from the raw payload (its schema default is hi-IN).
                explicit_locale = str(payload.get("locale") or "").strip()
                explicit_agent_id = inbound.agent_id
                if consult_ctx is not None:
                    # The consult AI leg session always runs the registered
                    # persona (persona_property), whatever agent_id the
                    # connector defaulted to.
                    explicit_agent_id = str(consult_ctx.get("persona") or "persona_property")
                resolved_pack_id, resolved_agent_id, resolved_locale = resolve_session_defaults(
                    default_pack_id=tenant_cfg.default_pack_id,
                    default_agent_id=tenant_cfg.default_agent_id,
                    default_locale=tenant_cfg.default_locale,
                    explicit_pack_id=inbound.pack_id,
                    explicit_agent_id=explicit_agent_id,
                    explicit_locale=explicit_locale,
                )
                session = BrainWSSession(
                    session_id=inbound.session_id,
                    borrower_id=inbound.borrower_id,
                    agent_id=resolved_agent_id,
                    pack_id=resolved_pack_id,
                    locale=resolved_locale,
                    tenant_id=tenant_id,
                    force_flow=force_flow,
                    borrower_context=borrower_context,
                    tap_only=parse_tap_only(borrower_context.get("tap_only")),
                    speaker_label=str(borrower_context.get("speaker_label", "") or ""),
                    parent_session_uuid=str(
                        borrower_context.get("parent_session_uuid", "") or ""
                    ),
                    started=True,
                )
                await outbound_push.register(inbound.session_id, ws, session)
                record: BorrowerRecord | None = None
                asr_language = resolve_asr_language(
                    None,
                    locale=session.locale,
                    borrower_context=borrower_context,
                )
                try:
                    record = await _persist_session_borrower(ws.app.state, session)
                    asr_language = resolve_asr_language(
                        record,
                        locale=session.locale,
                        borrower_context=borrower_context,
                    )
                    if record is not None and record.identity.get("name"):
                        borrower_context.setdefault(
                            "borrower_name", record.identity.get("name", "")
                        )
                except Exception:
                    logger.exception(
                        "brain ws session_start persist failed session_id=%s borrower_id=%s",
                        session.session_id,
                        session.borrower_id,
                    )
                borrower_name = str(
                    (record.identity.get("name") if record else "")
                    or borrower_context.get("borrower_name", "")
                )
                if settings.test_mode:
                    # SOT script is Hindi: force Sarvam to transcribe hi-IN regardless
                    # of the resolved DB borrower's stored language (which may be "en").
                    asr_language = "hi-IN"
                # W1-C C0 (DEBT-026): carry the tenant's dead-air apology line +
                # unknown_info-register voice to the go-server so DeadAirHandler
                # can speak it via TTS before clean-close on ASR-reconnect-
                # exhaustion. Open tenants (no profile) leave both empty → handler
                # closes silently.
                # F3 (PREDUE-007 residual): for tenants whose TTS voice is per-scenario
                # (PaisaLo: predue/ondue→priya, postdue1/2→neha, postdue3→kabir,
                # npa→amit), the static profile.voice_id is empty — resolve the
                # scenario voice from the hydrated borrower loan (dpd/npa) here so
                # DeadAirHandler speaks the apology in the SAME voice the call uses.
                from app.engine.tenant_profile import get_tenant_profile

                _ready_profile = get_tenant_profile(tenant_id)
                _apology_text = ""
                _apology_voice = ""
                if _ready_profile is not None:
                    _apology_text = _ready_profile.apology_dead_air or ""
                    _apology_voice = _ready_profile.voice_id or ""
                if not _apology_voice and record is not None and tenant_id == "paisalo":
                    _apology_voice = _resolve_plo_scenario_voice(record, settings)
                await _send_model(
                    ws,
                    SessionReadyMessage(
                        session_id=session.session_id,
                        borrower_id=session.borrower_id,
                        borrower_name=borrower_name,
                        asr_language=asr_language,
                        apology_text=_apology_text,
                        apology_voice_id=_apology_voice,
                    ),
                )
                logger.info(
                    "brain ws session_start session_id=%s borrower_id=%s agent_id=%s "
                    "tenant_id=%s force_flow=%s borrower_name=%s asr_language=%s "
                    "tap_only=%s speaker_label=%s tools_client=%s",
                    session.session_id,
                    session.borrower_id,
                    session.agent_id,
                    session.tenant_id,
                    session.force_flow or "",
                    borrower_name,
                    asr_language,
                    session.tap_only,
                    session.speaker_label,
                    (settings.tools_mode or "stub").lower(),
                )
                continue

            if session is None or not session.started or session.closed:
                logger.warning("brain ws message before session_start: %s", inbound.type)
                continue

            if inbound.session_id != session.session_id:
                logger.warning(
                    "brain ws session_id mismatch got=%s want=%s",
                    inbound.session_id,
                    session.session_id,
                )
                continue

            if isinstance(inbound, SessionEndMessage):
                session.closed = True
                logger.info(
                    "brain ws session_end session_id=%s tenant_id=%s",
                    session.session_id,
                    session.tenant_id,
                )
                break

            if isinstance(inbound, CancelMessage):
                session.cancel_turn(inbound.turn_id)
                logger.info(
                    "brain ws cancel session_id=%s tenant_id=%s turn_id=%s",
                    session.session_id,
                    session.tenant_id,
                    inbound.turn_id,
                )
                continue

            if isinstance(inbound, PlaybackDoneMessage):
                if session.tap_only:
                    continue
                # A reply finished playing to the caller. Two consumers:
                # a parked consult request waiting for its hold announcement,
                # and the no-input reprompt timer (starts counting only once
                # the caller has actually heard the question).
                tenant_cfg_now = tenant_config(session.tenant_id)
                if (
                    session.pending_consult_request is not None
                    and inbound.turn_id == session.consult_request_turn_id
                ):
                    logger.info(
                        "brain ws hold announcement played; starting consult "
                        "session_id=%s turn_id=%s",
                        session.session_id,
                        inbound.turn_id,
                    )
                    _launch_consult_start(ws, ws.app.state, session, tenant_cfg_now)
                elif (
                    session.pending_conference_join_request is not None
                    and inbound.turn_id == session.conference_join_request_turn_id
                ):
                    logger.info(
                        "brain ws connecting line played; starting conference_join "
                        "session_id=%s turn_id=%s",
                        session.session_id,
                        inbound.turn_id,
                    )
                    _launch_conference_join_start(
                        ws, ws.app.state, session, tenant_cfg_now
                    )
                elif (
                    session.consult_hold_announce_turn_id is not None
                    and inbound.turn_id == session.consult_hold_announce_turn_id
                ):
                    session.consult_hold_announce_turn_id = None
                    consult_id = pending_consult_id(session.session_id)
                    if consult_id:
                        await consult_hold_resume(consult_id)
                        logger.info(
                            "brain ws consult hold resumed after announcement "
                            "session_id=%s turn_id=%s consult_id=%s",
                            session.session_id,
                            inbound.turn_id,
                            consult_id,
                        )
                else:
                    _arm_noinput_timer(ws, ws.app.state, session, tenant_cfg_now)
                continue

            if isinstance(inbound, TurnMessage):
                # The caller spoke: any pending no-input escalation resets.
                _cancel_noinput_timer(session)
                session.noinput_count = 0

                async def _run(msg: TurnMessage = inbound) -> None:
                    await _run_turn(
                        ws,
                        ws.app.state,
                        session,
                        msg,
                        deadline_s=deadline_s,
                        fallback_text=fallback_text,
                    )

                await session.supersede_and_run(inbound, _run)
    except WebSocketDisconnect:
        logger.info(
            "brain ws disconnected session_id=%s tenant_id=%s",
            getattr(session, "session_id", None),
            acquired_tenant or "",
        )
    finally:
        if session is not None and session.inflight_turn_id:
            session.cancel_turn(session.inflight_turn_id)
        # Kill the consult-result watcher with the session (no push after end).
        if session is not None and session.consult_watch_task is not None:
            session.consult_watch_task.cancel()
        if session is not None and session.conference_join_watch_task is not None:
            session.conference_join_watch_task.cancel()
        # Kill deferred-consult and no-input timers with the session.
        if session is not None:
            _cancel_noinput_timer(session)
            for task in (
                session.consult_fallback_task,
                session.consult_start_task,
                session.conference_join_fallback_task,
                session.conference_join_start_task,
            ):
                if task is not None and not task.done():
                    task.cancel()
        # Prompt-mode history is in-memory per session; drop it with the session.
        if session is not None:
            await outbound_push.unregister(session.session_id)
            clear_prompt_session(session.session_id)
            # If this was a bound consult (property) leg, its binding is spent.
            consult_binding.unregister(session.session_id)
            if (
                not session.tap_only
                and session.tenant_id == "conference"
                and session.session_id
            ):
                finalize_conference(session.session_id)
        # Release the per-tenant concurrency slot on session_end/disconnect.
        if acquired_tenant is not None:
            SESSION_REGISTRY.release(acquired_tenant)
