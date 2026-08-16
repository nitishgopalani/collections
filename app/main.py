import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.logging_config import configure_logging
from app.clients.kb import create_kb_client
from app.clients.llm_vertex import create_llm_client
from app.clients.tools import create_tool_client
from app.config import get_settings
from app.startup_validation import validate_settings_or_exit
from app.engine.turn import handle_turn
from app.exceptions import StaleStateError
from app.flows.loader import get_flow_set
from app.flows.override_provider import create_override_provider
from app.memory.store import create_memory_store
from app.schemas.api import TurnRequest, TurnResponse
from app.ws.handler import handle_brain_websocket
from app.ws.conference_transcript import get_merged_transcript, get_store
from app.admin.v0 import router as admin_v0_router
from app.dialer.v0 import router as dialer_v0_router
from app.engine.drain import get_drain
from app.version import build_info

logger = logging.getLogger(__name__)

BRAIN_INTERNAL_SECRET_HEADER = "X-Brain-Internal-Secret"


def _transcript_internal_auth_ok(request: Request) -> bool:
    """When BRAIN_INTERNAL_SECRET is set, only orchestrator may read transcripts."""
    secret = (os.getenv("BRAIN_INTERNAL_SECRET") or "").strip()
    if not secret:
        return True
    return request.headers.get(BRAIN_INTERNAL_SECRET_HEADER) == secret


configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    validate_settings_or_exit(settings)
    app.state.llm = create_llm_client()
    app.state.kb = create_kb_client()
    app.state.tools = create_tool_client()
    app.state.memory = create_memory_store()
    bind = getattr(app.state.tools, "bind_source", None)
    if callable(bind):
        bind(app.state.memory)
    app.state.settings = settings
    app.state.flows = get_flow_set()
    app.state.overrides = create_override_provider()
    get_store().configure_ttl(settings.conference_transcript_ttl_s)
    logger.info(
        "collections-engine started stub_mode=%s memory_stub=%s borrower_db=%s "
        "kb_stub=%s tools_mode=%s llm_stub=%s",
        settings.stub_mode,
        settings.memory_stub_mode,
        settings.borrower_db_enabled,
        settings.kb_stub_mode,
        settings.tools_client_mode,
        settings.llm_stub_mode,
    )
    if settings.borrower_db_enabled:
        if await app.state.memory.ping():
            logger.info("borrower postgres connected (local test DB)")
        else:
            logger.error("borrower postgres configured but ping failed")

    def _arm_drain(*_args: object) -> None:
        get_drain().begin(cap_s=float(get_settings().drain_cap_s))

    try:
        import signal

        signal.signal(signal.SIGTERM, _arm_drain)
        signal.signal(signal.SIGINT, _arm_drain)
    except (ValueError, OSError):
        pass
    yield
    drain = get_drain()
    drain.begin(cap_s=float(settings.drain_cap_s))
    drain.wait_idle()


app = FastAPI(
    title="Collections Dialogue Engine",
    version="0.1.0",
    description="Text-in → text-out RBI-compliant collections dialogue engine",
    lifespan=lifespan,
)

_admin_settings = get_settings()
if _admin_settings.admin_api_enabled:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[_admin_settings.admin_cors_origin],
        allow_methods=["*"],
        allow_headers=["*"],
    )
app.include_router(admin_v0_router)
app.include_router(dialer_v0_router)


@app.get("/version")
async def version() -> dict[str, object]:
    return build_info()


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    settings = app.state.settings
    llm_ok = await app.state.llm.ping()
    kb_ok = await app.state.kb.ping()
    tools_ok = await app.state.tools.ping()
    memory_ok = await app.state.memory.ping()
    clients = {
        "llm": llm_ok,
        "kb": kb_ok,
        "tools": tools_ok,
        "memory": memory_ok,
    }
    if settings.borrower_db_enabled:
        clients["borrower_db"] = memory_ok
    all_ok = all(clients.values())
    tools_mode = getattr(app.state.tools, "mode", settings.tools_client_mode)
    client_modes = {
        "kb": "stub" if app.state.kb.is_stub else "live",
        "tools": tools_mode,
        "llm": "stub" if app.state.llm.is_stub else "live",
        "memory": "stub" if settings.memory_stub_mode else "live",
        "borrower_db": "postgres" if settings.borrower_db_enabled else "none",
    }
    return {
        "status": "ok" if all_ok else "degraded",
        "draining": get_drain().draining,
        "stub_mode": settings.stub_mode,
        "memory_stub_mode": settings.memory_stub_mode,
        "borrower_db_enabled": settings.borrower_db_enabled,
        "kb_stub_mode": settings.kb_stub_mode,
        "tools_stub_mode": settings.tools_stub_mode,
        "tools_mode": tools_mode,
        "llm_stub_mode": settings.llm_stub_mode,
        "client_modes": client_modes,
        "clients": clients,
    }


@app.post("/turn", response_model=TurnResponse)
async def turn(request: TurnRequest) -> TurnResponse:
    drain = get_drain()
    if drain.draining:
        existing = await app.state.memory.load_state(request.call_id)
        if existing is None:
            raise HTTPException(status_code=503, detail="draining")
    try:
        return await handle_turn(
            request,
            memory=app.state.memory,
            kb=app.state.kb,
            llm=app.state.llm,
            tools=app.state.tools,
            flows=app.state.flows,
            overrides=app.state.overrides,
        )
    except StaleStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/v1/conference/{parent_session_uuid}/transcript")
async def conference_transcript(parent_session_uuid: str, request: Request) -> dict[str, Any]:
    """CF2.3 merged per-speaker timeline for a conference (tap captures)."""
    if not _transcript_internal_auth_ok(request):
        req_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "code": "unauthorized",
                    "message": "missing or invalid internal transcript auth",
                    "request_id": req_id,
                }
            },
            headers={"X-Request-ID": req_id},
        )
    payload = get_merged_transcript(parent_session_uuid)
    if payload is None:
        req_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "code": "not_found",
                    "message": "conference transcript not found",
                    "request_id": req_id,
                }
            },
            headers={"X-Request-ID": req_id},
        )
    return payload


@app.websocket("/ws/brain")
async def brain_ws(websocket: WebSocket) -> None:
    """EB-6 persistent text contract — Go telephony client ↔ brain (no audio)."""
    settings = get_settings()
    if not settings.ws_enabled:
        await websocket.close(code=1008, reason="brain websocket disabled")
        return
    await handle_brain_websocket(websocket)
