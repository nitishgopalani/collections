"""Push unsolicited reply turns to an active brain WebSocket session.

Used when a detached background task (warm-transfer driver, consult watcher)
must speak a line without a caller turn — same chunk/flow_class/done contract
as handler._push_reply.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from app.schemas.ws_contract import ChunkMessage, DoneMessage, FlowClassMessage
from app.ws.session import BrainWSSession
from app.ws.chunking import chunk_reply_for_tts

logger = logging.getLogger(__name__)


@dataclass
class _ActiveSession:
    ws: WebSocket
    session: BrainWSSession


_REGISTRY: dict[str, _ActiveSession] = {}
_LOCK = asyncio.Lock()


async def register(session_id: str, ws: WebSocket, session: BrainWSSession) -> None:
    async with _LOCK:
        _REGISTRY[session_id] = _ActiveSession(ws=ws, session=session)


async def unregister(session_id: str) -> None:
    async with _LOCK:
        _REGISTRY.pop(session_id, None)


async def push_unsolicited_reply(
    session_id: str,
    text: str,
    *,
    disposition: str,
    end_call: bool = False,
    end_call_delay_ms: int = 0,
    turn_id_prefix: str = "push",
) -> bool:
    """Emit one unsolicited turn to the live session. Returns False if absent/closed."""
    import uuid

    async with _LOCK:
        entry = _REGISTRY.get(session_id)
    if entry is None:
        logger.warning(
            "outbound push skipped: no active ws session_id=%s disposition=%s",
            session_id,
            disposition,
        )
        return False
    ws, session = entry.ws, entry.session
    if session.closed or ws.client_state != WebSocketState.CONNECTED:
        logger.warning(
            "outbound push skipped: ws closed session_id=%s disposition=%s",
            session_id,
            disposition,
        )
        return False
    turn_id = f"{turn_id_prefix}-{uuid.uuid4().hex[:8]}"
    from app.ws.handler import _send_model  # lazy: avoid import cycle with handler

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
    logger.info(
        "outbound push sent session_id=%s turn_id=%s disposition=%s end_call=%s delay_ms=%s",
        session_id,
        turn_id,
        disposition,
        end_call,
        end_call_delay_ms,
    )
    return True
