import pytest
from pydantic import ValidationError

from app.schemas.api import TurnRequest, TurnResponse
from app.schemas.command import Command
from app.schemas.flow import FlowSet
from app.schemas.state import BorrowerRecord, ConversationState, Event, Frame


def test_turn_request_valid():
    req = TurnRequest(
        call_id="c1",
        tenant_id="default",
        borrower_id="b1",
        transcript="hello",
    )
    assert req.channel == "voice"
    assert req.locale == "hi-IN"


def test_turn_request_rejects_empty_call_id():
    with pytest.raises(ValidationError):
        TurnRequest(call_id="", tenant_id="t", borrower_id="b", transcript="x")


def test_command_vocabulary():
    cmd = Command(command="start_flow", flow="promise_to_pay")
    assert cmd.command == "start_flow"
    assert cmd.flow == "promise_to_pay"


def test_conversation_state_defaults():
    state = ConversationState(call_id="c", tenant_id="t", borrower_id="b")
    assert state.version == 0
    assert state.flow_stack == []


def test_borrower_record_blocks():
    record = BorrowerRecord(borrower_id="b1")
    assert record.trust_current == 50
    assert "vulnerable" not in record.compliance_flags


def test_event_and_frame():
    event = Event(ts="2026-01-01T00:00:00Z", kind="command", data={"cmd": "clarify"})
    frame = Frame(flow="dispute", step_index=1, parked=True)
    assert event.kind == "command"
    assert frame.parked is True


def test_turn_response_fields():
    resp = TurnResponse(reply_text="ok", audit_id="audit-1")
    assert resp.end_call is False
    assert resp.transfer_to_human is False


def test_flow_set_empty():
    fs = FlowSet(flows={})
    assert fs.flows == {}
