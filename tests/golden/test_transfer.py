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
async def test_transfer_call_keeps_ai_leg_up_when_warm_ready(monkeypatch):
    """Warm transfer configured (orchestrator + agent number): the AI leg must
    STAY UP until the agent joins — its death tears down the whole Stasis-owned
    call — so end_call is NOT set."""
    from app.config import get_settings

    monkeypatch.setenv("ORCHESTRATOR_BASE_URL", "http://127.0.0.1:8095")
    monkeypatch.setenv("TRANSFER_AGENT_NUMBER", "9810001192")
    get_settings.cache_clear()
    try:
        state = new_conversation_state("call-t2", "salary_on_time", "b-t2")
        state.flow_stack = [Frame(flow="sot_commit", step_index=0)]
        state = apply(state, [Command(command="start_flow", flow="sot_obj_no_timeline")])
        runner = make_async_action_runner(FakeToolClient())
        result = await run_executor_async(state, FLOWS, runner)
    finally:
        get_settings.cache_clear()  # don't leak the env into other tests

    assert result.transfer_to_human is True
    assert result.end_call is False
    slots = result.state.slots
    assert slots.get("transfer_requested") is True
    assert slots.get("sot_call_closed") is True  # script over; no restart on barge-in


@pytest.mark.asyncio
async def test_transfer_call_stub_when_no_agent_number(monkeypatch):
    """Orchestrator URL alone is not warm-ready — without an agent number the
    hook would stub, so the action must still end the bot leg."""
    from app.config import get_settings

    monkeypatch.setenv("ORCHESTRATOR_BASE_URL", "http://127.0.0.1:8095")
    monkeypatch.delenv("TRANSFER_AGENT_NUMBER", raising=False)
    get_settings.cache_clear()
    try:
        state = new_conversation_state("call-t3", "salary_on_time", "b-t3")
        state.flow_stack = [Frame(flow="sot_commit", step_index=0)]
        state = apply(state, [Command(command="start_flow", flow="sot_obj_no_timeline")])
        runner = make_async_action_runner(FakeToolClient())
        result = await run_executor_async(state, FLOWS, runner)
    finally:
        get_settings.cache_clear()

    assert result.end_call is True


# ---------------------------------------------------------------------------
# transfer_caller_id — empty flow slot must fall back to TRANSFER_CALLER_ID
# (an anonymous agent-leg dial is carrier-rejected with SIP 480: 2026-07-07)
# ---------------------------------------------------------------------------


def test_transfer_caller_id_slot_wins(monkeypatch):
    monkeypatch.setenv("TRANSFER_CALLER_ID", "1725617002")
    assert turn_mod.transfer_caller_id("9998887776") == "9998887776"


def test_transfer_caller_id_empty_slot_uses_env(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("TRANSFER_CALLER_ID", "1725617002")
    get_settings.cache_clear()
    try:
        assert turn_mod.transfer_caller_id("") == "1725617002"
        assert turn_mod.transfer_caller_id(None) == "1725617002"
    finally:
        get_settings.cache_clear()


def test_transfer_caller_id_unset_everywhere_warns_and_returns_empty(monkeypatch, caplog):
    from app.config import get_settings

    monkeypatch.delenv("TRANSFER_CALLER_ID", raising=False)
    get_settings.cache_clear()
    try:
        with caplog.at_level("WARNING"):
            assert turn_mod.transfer_caller_id("") == ""
        assert any("TRANSFER_CALLER_ID" in r.message for r in caplog.records)
    finally:
        get_settings.cache_clear()


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
    assert rec.calls[0][0] == "warm_transfer"
    warm_kw = rec.calls[0][1]
    assert warm_kw["session_uuid"] == "sess-1"
    assert warm_kw["to"] == "9810001192"
    assert warm_kw["caller_id"] == "1725617001"
    assert "ring_budget_s" in warm_kw
    assert rec.names()[-1] == "transfer_complete"
    assert "transfer_cancel" not in rec.names()
    assert "hangup" not in rec.names()


@pytest.mark.asyncio
async def test_driver_no_answer_pushes_spoken_close_then_graceful_hangup(monkeypatch):
    """Ring budget exhausted -> cancel, spoken close via push (end_call + grace),
    no orchestrator customer hangup when the push succeeds."""
    rec = RecordingOrchestrator([])  # always "ringing"
    _patch_driver(monkeypatch, rec)
    push_calls: list[dict] = []

    async def _fake_push(session_id, text, **kwargs):
        push_calls.append({"session_id": session_id, "text": text, **kwargs})
        return True

    import app.ws.outbound_push as outbound_push

    monkeypatch.setattr(outbound_push, "push_unsolicited_reply", _fake_push)

    await turn_mod._drive_warm_transfer(
        0.0,
        session_uuid="sess-2",
        target="9810001192",
        caller_id="",
        reason="handoff",
        answer_budget_s=0.05,
        complete_delay_s=0.0,
        no_answer_reply="Maaf kijiye, agent uplabdh nahin.",
        end_call_grace_ms=700,
    )
    names = rec.names()
    assert names.index("transfer_cancel") < len(names)
    assert "hangup" not in names
    assert "transfer_complete" not in names
    assert len(push_calls) == 1
    assert push_calls[0]["session_id"] == "sess-2"
    assert push_calls[0]["text"] == "Maaf kijiye, agent uplabdh nahin."
    assert push_calls[0]["disposition"] == "TRANSFER_NO_ANSWER"
    assert push_calls[0]["end_call"] is True
    assert push_calls[0]["end_call_delay_ms"] == 700


@pytest.mark.asyncio
async def test_driver_no_answer_falls_back_to_hangup_when_push_fails(monkeypatch):
    """If the WS session is gone, fall back to orchestrator customer hangup."""
    rec = RecordingOrchestrator([])
    _patch_driver(monkeypatch, rec)

    async def _fail_push(*_a, **_k):
        return False

    import app.ws.outbound_push as outbound_push

    monkeypatch.setattr(outbound_push, "push_unsolicited_reply", _fail_push)

    await turn_mod._drive_warm_transfer(
        0.0,
        session_uuid="sess-2b",
        target="9810001192",
        caller_id="",
        reason="handoff",
        answer_budget_s=0.05,
        complete_delay_s=0.0,
        no_answer_reply="Line.",
        end_call_grace_ms=700,
    )
    assert "transfer_cancel" in rec.names()
    assert ("hangup", {"channel_id": "cust-1"}) in rec.calls


@pytest.mark.asyncio
async def test_driver_agent_busy_pushes_spoken_close(monkeypatch):
    """Orchestrator reports failed (busy/declined): spoken close, no cancel."""
    rec = RecordingOrchestrator(["failed"])
    _patch_driver(monkeypatch, rec)
    push_calls: list[dict] = []

    async def _fake_push(session_id, text, **kwargs):
        push_calls.append({"session_id": session_id, "text": text, **kwargs})
        return True

    import app.ws.outbound_push as outbound_push

    monkeypatch.setattr(outbound_push, "push_unsolicited_reply", _fake_push)

    await turn_mod._drive_warm_transfer(
        0.0,
        session_uuid="sess-3",
        target="9810001192",
        caller_id="",
        reason="handoff",
        answer_budget_s=1.0,
        complete_delay_s=0.0,
        no_answer_reply="Agent busy line.",
        end_call_grace_ms=700,
    )
    names = rec.names()
    assert "transfer_cancel" not in names
    assert "hangup" not in names
    assert len(push_calls) == 1
    assert push_calls[0]["disposition"] == "TRANSFER_NO_ANSWER"


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
