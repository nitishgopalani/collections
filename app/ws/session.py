"""Per-call brain WebSocket session state (EB-6)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass
class BrainWSSession:
    session_id: str
    borrower_id: str
    agent_id: str
    pack_id: str = ""
    locale: str = "hi-IN"
    tenant_id: str = "default"
    force_flow: str | None = None
    borrower_context: dict[str, Any] = field(default_factory=dict)
    # CF2.2 transcript-only tap listener (per-speaker snoop sessions).
    tap_only: bool = False
    speaker_label: str = ""
    parent_session_uuid: str = ""
    started: bool = False
    closed: bool = False
    inflight_turn_id: str | None = None
    inflight_task: asyncio.Task[Any] | None = None
    cancel_events: dict[str, asyncio.Event] = field(default_factory=dict)
    cancelled_turns: set[str] = field(default_factory=set)
    # Serializes outbound chunk/flow_class/done emission so an unsolicited
    # consult-result push can never interleave with a turn's reply frames.
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Background consult-result watcher (prompt mode); cancelled on session end.
    consult_watch_task: asyncio.Task[Any] | None = None
    # Deferred consult request (prompt mode): <consult ...> marker attrs waiting
    # for the hold announcement's playback_done before the property is dialled.
    pending_consult_request: dict[str, str] | None = None
    consult_request_turn_id: str | None = None
    consult_fallback_task: asyncio.Task[Any] | None = None
    consult_start_task: asyncio.Task[Any] | None = None
    # Turn id of an in-flight hold announcement (interim/final during consult).
    # playback_done triggers consult hold-resume on the orchestrator.
    consult_hold_announce_turn_id: str | None = None
    # CF1.5 deferred conference join (prompt mode conference tenant).
    pending_conference_join_request: dict[str, str] | None = None
    conference_join_request_turn_id: str | None = None
    conference_join_fallback_task: asyncio.Task[Any] | None = None
    conference_join_start_task: asyncio.Task[Any] | None = None
    conference_join_watch_task: asyncio.Task[Any] | None = None
    # No-input reprompt state (prompt mode): armed on playback_done, cancelled
    # by the next caller turn.
    last_reply_text: str = ""
    noinput_count: int = 0
    noinput_task: asyncio.Task[Any] | None = None

    def register_turn(self, turn_id: str) -> asyncio.Event:
        event = asyncio.Event()
        self.cancel_events[turn_id] = event
        self.inflight_turn_id = turn_id
        return event

    def clear_turn(self, turn_id: str) -> None:
        self.cancel_events.pop(turn_id, None)
        if self.inflight_turn_id == turn_id:
            self.inflight_turn_id = None
        if self.inflight_task is not None and self.inflight_task.done():
            self.inflight_task = None

    def cancel_turn(self, turn_id: str) -> None:
        self.cancelled_turns.add(turn_id)
        event = self.cancel_events.get(turn_id)
        if event is not None:
            event.set()
        if self.inflight_turn_id == turn_id and self.inflight_task is not None:
            self.inflight_task.cancel()

    def is_cancelled(self, turn_id: str) -> bool:
        return turn_id in self.cancelled_turns

    async def supersede_and_run(
        self,
        msg: Any,
        run_fn: Callable[[Any], Awaitable[None]],
    ) -> None:
        """Cancel any in-flight turn and start processing the latest caller turn."""
        if self.inflight_turn_id and self.inflight_turn_id != msg.turn_id:
            stale = self.inflight_turn_id
            self.cancel_turn(stale)
            logger.info(
                "brain ws superseding stale turn session_id=%s stale_turn_id=%s new_turn_id=%s",
                self.session_id,
                stale,
                msg.turn_id,
            )
        if self.inflight_task is not None and not self.inflight_task.done():
            self.inflight_task.cancel()
            try:
                await self.inflight_task
            except asyncio.CancelledError:
                pass

        task = asyncio.create_task(run_fn(msg))
        self.inflight_task = task

        def _done(t: asyncio.Task[Any]) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.warning(
                    "brain ws turn task failed session_id=%s: %s",
                    self.session_id,
                    exc,
                )

        task.add_done_callback(_done)
