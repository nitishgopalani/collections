"""Tests for brain ws session supersede behaviour."""

import asyncio

import pytest

from app.schemas.ws_contract import TurnMessage
from app.ws.session import BrainWSSession


@pytest.mark.asyncio
async def test_supersede_and_run_cancels_stale_task():
    session = BrainWSSession(session_id="sess", borrower_id="b", agent_id="a")
    started: list[str] = []
    cancelled: list[str] = []

    async def slow_turn(msg: TurnMessage) -> None:
        started.append(msg.turn_id)
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(msg.turn_id)
            raise

    msg1 = TurnMessage(
        session_id="sess",
        turn_id="turn-1",
        transcript="first",
        flow_class="Default",
    )
    msg2 = TurnMessage(
        session_id="sess",
        turn_id="turn-2",
        transcript="second",
        flow_class="Default",
    )

    await session.supersede_and_run(msg1, slow_turn)
    await asyncio.sleep(0.05)
    await session.supersede_and_run(msg2, slow_turn)
    await asyncio.sleep(0.05)

    assert started == ["turn-1", "turn-2"]
    assert "turn-1" in cancelled
    if session.inflight_task is not None:
        session.inflight_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await session.inflight_task
