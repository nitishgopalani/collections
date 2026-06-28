"""Unit tests for borrower context and agent routing."""

from app.schemas.state import ConversationState
from app.ws.borrower_context import apply_borrower_context_to_state, normalize_borrower_context
from app.ws.routing import resolve_agent_routing


def test_normalize_borrower_context_maps_phone_and_amount():
    ctx = normalize_borrower_context(
        {
            "borrower_name": "Rajesh",
            "customer_phone": "919876543210",
            "amount_due": 350,
        }
    )
    assert ctx["borrower_name"] == "Rajesh"
    assert ctx["phone"] == "919876543210"
    assert ctx["amount_due"] == 350


def test_apply_borrower_context_to_state():
    state = ConversationState(call_id="c", tenant_id="t", borrower_id="b")
    updated = apply_borrower_context_to_state(
        state,
        {"borrower_name": "Rajesh", "amount_due": 350},
    )
    assert updated.slots["borrower_name"] == "Rajesh"
    assert updated.slots["amount_due"] == 350


def test_agent_routing_identity_name_confirm():
    flow, tenant = resolve_agent_routing("identity-name-confirm")
    assert flow == "identity_name_confirm"
    assert tenant == "test-name-identity"


def test_agent_routing_default_unchanged():
    flow, tenant = resolve_agent_routing("agent-1")
    assert flow is None
    assert tenant is None
