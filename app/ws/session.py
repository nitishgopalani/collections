"""Per-call brain WebSocket session state (EB-6)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class BrainWSSession:
    session_id: str
    borrower_id: str
    agent_id: str
    pack_id: str = ""
    locale: str = "hi-IN"
    tenant_id: str = "default"
    started: bool = False
    closed: bool = False
    inflight_turn_id: str | None = None
    inflight_task: asyncio.Task[Any] | None = None
    cancel_events: dict[str, asyncio.Event] = field(default_factory=dict)
    cancelled_turns: set[str] = field(default_factory=set)

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
