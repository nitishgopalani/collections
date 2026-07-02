"""Live call transfer (Model A): swappable provider + transfer_call intent."""

import pytest

from app.clients.tools_sim import FakeToolClient
from app.clients.transfer import (
    DISP_PENDING,
    TransferResult,
    _build_payload,
    initiate_transfer,
)
from app.engine.actions import make_async_action_runner
from app.engine.executor import run_async as run_executor_async
from app.engine.tracker import apply, new_conversation_state
from app.flows.loader import load_all_flows
from app.schemas.command import Command
from app.schemas.state import Frame

FLOWS = load_all_flows()


# ---------------------------------------------------------------------------
# Provider — stub by default (no endpoint configured yet)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stub_returns_pending():
    result = await initiate_transfer(
        call_id="call-1", target="", reason="handoff"
    )
    assert isinstance(result, TransferResult)
    assert result.status == "pending"
    assert result.disposition == DISP_PENDING
    assert result.ok is True


def test_build_payload_includes_fields():
    payload = _build_payload(call_id="c1", target="queue-a", reason="no_timeline")
    assert payload["call_id"] == "c1"
    assert payload["reason"] == "no_timeline"
    assert payload["target"] == "queue-a"


def test_build_payload_omits_empty_target():
    payload = _build_payload(call_id="c1", target="", reason="handoff")
    assert "target" not in payload


def test_transfer_result_ok_semantics():
    assert TransferResult("pending", DISP_PENDING).ok is True
    assert TransferResult("initiated", "TRANSFERRED").ok is True
    assert TransferResult("failed", "TRANSFER_FAILED").ok is False


# ---------------------------------------------------------------------------
# transfer_call action — declares intent + ends the bot leg (no network here)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transfer_call_action_declares_intent():
    """sot_obj_no_timeline speaks the connect line then requests a transfer."""
    state = new_conversation_state("call-t", "salary_on_time", "b-t")
    state.flow_stack = [Frame(flow="sot_commit", step_index=0)]
    state = apply(state, [Command(command="start_flow", flow="sot_obj_no_timeline")])
    runner = make_async_action_runner(FakeToolClient())
    result = await run_executor_async(state, FLOWS, runner)

    assert result.reply_id == "sot_obj_no_timeline"
    assert result.transfer_to_human is True
    assert result.end_call is True
    slots = result.state.slots
    assert slots.get("transfer_requested") is True
    assert slots.get("disposition") == "TRANSFER_PENDING"
    # The async bridge (transfer_initiated/status) is set later by the turn hook,
    # not by the sync action itself.
    assert slots.get("transfer_initiated") is None
