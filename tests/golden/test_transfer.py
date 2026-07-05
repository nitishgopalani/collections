"""Warm transfer (orchestrator-only): transfer_call intent + the async driver.

The legacy voip.ivrobd.com carrier POST (app/clients/transfer.py) is REMOVED —
the only transfer path is the ari-orchestrator warm handoff: dial the agent,
three-way on answer, drop the AI leg on transfer/complete.
"""

import asyncio

import pytest

import app.clients.orchestrator as orch
import app.engine.turn as turn_mod
from app.clients.tools_sim import FakeToolClient
from app.engine.actions import make_async_action_runner
from app.engine.executor import run_async as run_executor_async
from app.engine.tracker import apply, new_conversation_state
from app.flows.loader import load_all_flows
from app.schemas.command import Command
from app.schemas.state import Frame

FLOWS = load_all_flows()


# ---------------------------------------------------------------------------
# transfer_call action — declares intent; end_call only in stub mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transfer_call_action_declares_intent_stub():
    """No orchestrator configured: the handoff line plays and the bot leg ends
    (legacy stub behaviour), with the transfer request recorded."""
    state = new_conversation_state("call-t", "salary_on_time", "b-t")
    state.flow_stack = [Frame(flow="sot_commit", step_index=0)]
    state = apply(state, [Command(command="start_flow", flow="sot_obj_no_timeline")])
    runner = make_async_action_runner(FakeToolClient())
    result = await run_executor_async(state, FLOWS, runner)

    assert result.reply_id == "sot_obj_no_timeline"
    assert result.transfer_to_human is True
    assert result.end_call is True  # stub: no orchestrator -> end the bot leg
    slots = result.state.slots
    assert slots.get("transfer_requested") is True
    assert slots.get("disposition") == "TRANSFER_PENDING"
    # The driver launch (transfer_initiated/status) belongs to the turn hook,
    # not the sync action itself.
    assert slots.get("transfer_initiated") is None


@pytest.mark.asyncio
async def test_transfer_call_keeps_ai_leg_up_when_orchestrator_configured(monkeypatch):
    """Warm transfer: the AI leg must STAY UP until the agent joins — its death
    tears down the whole Stasis-owned call — so end_call is NOT set."""
    monkeypatch.setenv("ORCHESTRATOR_BASE_URL", "http://127.0.0.1:8095")
    state = new_conversation_state("call-t2", "salary_on_time", "b-t2")
    state.flow_stack = [Frame(flow="sot_commit", step_index=0)]
    state = apply(state, [Command(command="start_flow", flow="sot_obj_no_timeline")])
    runner = make_async_action_runner(FakeToolClient())
    result = await run_executor_async(state, FLOWS, runner)

    assert result.transfer_to_human is True
    assert result.end_call is False
    slots = result.state.slots
    assert slots.get("transfer_requested") is True
    assert slots.get("sot_call_closed") is True  # script over; no restart on barge-in


# ---------------------------------------------------------------------------
# _drive_warm_transfer — start -> poll -> complete/cancel (orchestrator mocked)
# ---------------------------------------------------------------------------


class RecordingOrchestrator:
    """Stands in for app.clients.orchestrator; scripts the status sequence."""

    def __init__(self, statuses: list[str]):
        self.calls: list[tuple[str, dict]] = []
        self._statuses = list(statuses)

    def warm_transfer(self, **kwargs):
        self.calls.append(("warm_transfer", kwargs))
        return {
            "transfer_id": "transfer-abc",
            "session_uuid": kwargs["session_uuid"],
            "bridge_id": "br-1",
            "channel_ids": ["cust-1", "transfer-abc-leg"],
            "agent_channel_id": "transfer-abc-leg",
            "status": "originating",
        }

    def transfer_status(self, **kwargs):
        self.calls.append(("transfer_status", kwargs))
        status = self._statuses.pop(0) if self._statuses else "ringing"
        return {
            "transfer_id": kwargs["transfer_id"],
            "status": status,
            "customer_channel_id": "cust-1",
            "agent_channel_id": "transfer-abc-leg",
        }

    def transfer_complete(self, **kwargs):
        self.calls.append(("transfer_complete", kwargs))
        return {"transfer_id": kwargs["transfer_id"], "status": "completed"}

    def transfer_cancel(self, **kwargs):
        self.calls.append(("transfer_cancel", kwargs))
        return {"transfer_id": kwargs["transfer_id"], "status": "cancelled"}

    def hangup(self, **kwargs):
        self.calls.append(("hangup", kwargs))
        return {"ok": True}

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def _patch_driver(monkeypatch, rec: RecordingOrchestrator) -> None:
    monkeypatch.setattr(orch, "warm_transfer", rec.warm_transfer)
    monkeypatch.setattr(orch, "transfer_status", rec.transfer_status)
    monkeypatch.setattr(orch, "transfer_complete", rec.transfer_complete)
    monkeypatch.setattr(orch, "transfer_cancel", rec.transfer_cancel)
    monkeypatch.setattr(orch, "hangup", rec.hangup)
    monkeypatch.setattr(turn_mod, "_TRANSFER_POLL_S", 0.01)


@pytest.mark.asyncio
async def test_driver_happy_path_completes(monkeypatch):
    """Agent answers -> transfer/complete drops the AI leg; nobody hung up."""
    rec = RecordingOrchestrator(["ringing", "up"])
    _patch_driver(monkeypatch, rec)

    await turn_mod._drive_warm_transfer(
        0.0,
        session_uuid="sess-1",
        target="9810001192",
        caller_id="1725617001",
        reason="sot_pre_closure_handoff",
        answer_budget_s=1.0,
        complete_delay_s=0.0,
    )
    assert rec.calls[0] == (
        "warm_transfer",
        {"session_uuid": "sess-1", "transfer_to": "9810001192", "caller_id": "1725617001"},
    )
    assert rec.names()[-1] == "transfer_complete"
    assert "transfer_cancel" not in rec.names()
    assert "hangup" not in rec.names()


@pytest.mark.asyncio
async def test_driver_no_answer_cancels_and_ends_call(monkeypatch):
    """Ring budget exhausted -> cancel, then hang up the customer leg (the flow
    already closed; nothing more can be spoken)."""
    rec = RecordingOrchestrator([])  # always "ringing"
    _patch_driver(monkeypatch, rec)

    await turn_mod._drive_warm_transfer(
        0.0,
        session_uuid="sess-2",
        target="9810001192",
        caller_id="",
        reason="handoff",
        answer_budget_s=0.05,
        complete_delay_s=0.0,
    )
    names = rec.names()
    assert "transfer_cancel" in names
    assert ("hangup", {"channel_id": "cust-1"}) in rec.calls
    assert "transfer_complete" not in names


@pytest.mark.asyncio
async def test_driver_agent_busy_ends_call_without_cancel(monkeypatch):
    """Orchestrator reports failed (busy/declined): no cancel needed, the call
    is ended via the customer leg."""
    rec = RecordingOrchestrator(["failed"])
    _patch_driver(monkeypatch, rec)

    await turn_mod._drive_warm_transfer(
        0.0,
        session_uuid="sess-3",
        target="9810001192",
        caller_id="",
        reason="handoff",
        answer_budget_s=1.0,
        complete_delay_s=0.0,
    )
    names = rec.names()
    assert "transfer_cancel" not in names
    assert ("hangup", {"channel_id": "cust-1"}) in rec.calls
    assert "transfer_complete" not in names


@pytest.mark.asyncio
async def test_driver_customer_hung_up_mid_ring_is_noop(monkeypatch):
    """Orchestrator already tore the call down (finished): nothing to do."""
    rec = RecordingOrchestrator(["finished"])
    _patch_driver(monkeypatch, rec)

    await turn_mod._drive_warm_transfer(
        0.0,
        session_uuid="sess-4",
        target="9810001192",
        caller_id="",
        reason="handoff",
        answer_budget_s=1.0,
        complete_delay_s=0.0,
    )
    names = rec.names()
    assert "transfer_cancel" not in names
    assert "transfer_complete" not in names
    assert "hangup" not in names


@pytest.mark.asyncio
async def test_driver_swallows_errors(monkeypatch):
    """A dead orchestrator must never surface an error from the detached task."""

    def _boom(**kwargs):
        raise orch.OrchestratorError("orchestrator down")

    monkeypatch.setattr(orch, "warm_transfer", _boom)
    # Should not raise.
    await turn_mod._drive_warm_transfer(
        0.0,
        session_uuid="sess-5",
        target="t",
        caller_id="",
        reason="r",
        answer_budget_s=0.05,
        complete_delay_s=0.0,
    )


@pytest.mark.asyncio
async def test_driver_hold_delays_dial(monkeypatch):
    """The hold runs before the transfer POST so the handoff line plays first."""
    rec = RecordingOrchestrator(["up"])
    _patch_driver(monkeypatch, rec)

    loop = asyncio.get_running_loop()
    started = loop.time()
    await turn_mod._drive_warm_transfer(
        0.05,
        session_uuid="sess-6",
        target="9810001192",
        caller_id="",
        reason="handoff",
        answer_budget_s=1.0,
        complete_delay_s=0.0,
    )
    assert loop.time() - started >= 0.05
    assert rec.names()[0] == "warm_transfer"
