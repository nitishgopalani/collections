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
from app.engine.turn import handle_turn
from app.schemas.api import TurnRequest
from app.schemas.state import BorrowerRecord
from app.schemas.ws_contract import (
    CancelMessage,
    ChunkMessage,
    DoneMessage,
    ErrorMessage,
    FlowClassMessage,
    SessionEndMessage,
    SessionReadyMessage,
    SessionStartMessage,
    TurnMessage,
    parse_go_inbound,
)
from app.ws.borrower_context import normalize_borrower_context
from app.ws.borrower_resolve import resolve_asr_language, resolve_session_borrower
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


def _normalize_test_session_start(payload: dict[str, Any], settings: Any) -> tuple[dict[str, Any], bool]:
    """Fill bare session_start fields when TEST_MODE is on; return (payload, was_bare)."""
    if not settings.test_mode or payload.get("type") != "session_start":
        return payload, False
    normalized = dict(payload)
    was_bare = False
    if not str(normalized.get("session_id") or "").strip():
        normalized["session_id"] = str(uuid.uuid4())
        was_bare = True
    if not str(normalized.get("borrower_id") or "").strip():
        normalized["borrower_id"] = "sot_test_borrower"
        was_bare = True
    if not str(normalized.get("agent_id") or "").strip():
        normalized["agent_id"] = "salary-on-time-test"
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


async def _run_turn(
    ws: WebSocket,
    app_state: Any,
    session: BrainWSSession,
    msg: TurnMessage,
    *,
    deadline_s: float,
    fallback_text: str,
) -> None:
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

    async def _emit_gated_chunks(reply_text: str) -> None:
        """Stream gated reply chunks to Go before persist completes (gate already passed)."""
        for seq, text in enumerate(chunk_reply_for_tts(reply_text)):
            if cancel_event.is_set() or session.is_cancelled(msg.turn_id):
                return
            await _send_model(ws, ChunkMessage(turn_id=msg.turn_id, seq=seq, text=text))

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
                if settings.test_mode:
                    # TEST_MODE server: the upstream media service controls agent_id
                    # (we can't set it during testing), so pin every call to the
                    # salary_on_time test routing -> tenant + sot_opener entry flow.
                    force_flow, routed_tenant = resolve_agent_routing("salary-on-time-test")
                if test_bare_session:
                    tenant_id, tenant_source = settings.test_tenant_id, "test_bare"
                elif settings.test_mode:
                    # Keep TEST_MODE pinned to the routed test tenant regardless of any
                    # client_id, so existing deterministic test runs are unchanged.
                    tenant_id = routed_tenant or settings.test_tenant_id
                    tenant_source = "test_mode_routing"
                else:
                    tenant_id, tenant_source = resolve_session_tenant(
                        client_id=inbound.client_id,
                        routed_tenant=routed_tenant,
                        inbound_tenant_id=inbound.tenant_id,
                        default_tenant_id=settings.default_tenant_id,
                    )
                logger.info(
                    "brain ws tenant resolved session_id=%s tenant_id=%s source=%s client_id=%s",
                    inbound.session_id,
                    tenant_id,
                    tenant_source,
                    inbound.client_id or "",
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
                resolved_pack_id, resolved_agent_id, resolved_locale = resolve_session_defaults(
                    default_pack_id=tenant_cfg.default_pack_id,
                    default_agent_id=tenant_cfg.default_agent_id,
                    default_locale=tenant_cfg.default_locale,
                    explicit_pack_id=inbound.pack_id,
                    explicit_agent_id=inbound.agent_id,
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
                    started=True,
                )
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
                await _send_model(
                    ws,
                    SessionReadyMessage(
                        session_id=session.session_id,
                        borrower_id=session.borrower_id,
                        borrower_name=borrower_name,
                        asr_language=asr_language,
                    ),
                )
                logger.info(
                    "brain ws session_start session_id=%s borrower_id=%s agent_id=%s "
                    "tenant_id=%s force_flow=%s borrower_name=%s asr_language=%s",
                    session.session_id,
                    session.borrower_id,
                    session.agent_id,
                    session.tenant_id,
                    session.force_flow or "",
                    borrower_name,
                    asr_language,
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

            if isinstance(inbound, TurnMessage):

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
        # Release the per-tenant concurrency slot on session_end/disconnect.
        if acquired_tenant is not None:
            SESSION_REGISTRY.release(acquired_tenant)
