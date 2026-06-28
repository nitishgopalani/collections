import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket

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

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    validate_settings_or_exit(settings)
    app.state.llm = create_llm_client()
    app.state.kb = create_kb_client()
    app.state.tools = create_tool_client()
    app.state.memory = create_memory_store()
    app.state.settings = settings
    app.state.flows = get_flow_set()
    app.state.overrides = create_override_provider()
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
    yield


app = FastAPI(
    title="Collections Dialogue Engine",
    version="0.1.0",
    description="Text-in → text-out RBI-compliant collections dialogue engine",
    lifespan=lifespan,
)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
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
    borrower_db_ok: bool | None = None
    if settings.borrower_db_enabled:
        borrower_db_ok = memory_ok
        clients["borrower_db"] = borrower_db_ok
    all_ok = all(clients.values())
    settings = app.state.settings
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


@app.websocket("/ws/brain")
async def brain_ws(websocket: WebSocket) -> None:
    """EB-6 persistent text contract — Go telephony client ↔ brain (no audio)."""
    settings = get_settings()
    if not settings.ws_enabled:
        await websocket.close(code=1008, reason="brain websocket disabled")
        return
    await handle_brain_websocket(websocket)
