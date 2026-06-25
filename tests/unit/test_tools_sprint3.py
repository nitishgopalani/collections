import pytest

from app.clients.tools_sim import FakeToolClient
from app.engine.actions import ActionRegistry, _idempotency_key, make_action_runner
from app.engine.executor import run
from app.engine.tracker import apply, new_conversation_state
from app.exceptions import ToolInvocationError
from app.flows.loader import load_all_flows
from app.schemas.command import Command
from tests.fixtures.test_borrowers import B_DUE, B_PAID, B_VULNERABLE

FLOWS = load_all_flows()


@pytest.fixture
def sim_tools():
    client = FakeToolClient()
    client.reset()
    return client


@pytest.mark.asyncio
async def test_read_check_last_payment_paid_vs_due(sim_tools):
    registry = ActionRegistry(sim_tools)
    paid_state = new_conversation_state("c1", "default", B_PAID)
    due_state = new_conversation_state("c2", "default", B_DUE)

    paid = await registry.run_async("verify_payment", paid_state)
    due = await registry.run_async("verify_payment", due_state)

    assert paid.slots["payment_found"] is True
    assert due.slots["payment_found"] is False


@pytest.mark.asyncio
async def test_write_idempotency_same_key_one_effect(sim_tools):
    state = new_conversation_state("c-idem", "default", B_DUE)
    state.slots["amount_due"] = 5000
    args = {"borrower_id": B_DUE, "loan_id": None, "amount": 5000}
    key = _idempotency_key("c-idem", "create_payment_link", args)

    first = await sim_tools.invoke("create_payment_link", args, "default", idempotency_key=key)
    second = await sim_tools.invoke("create_payment_link", args, "default", idempotency_key=key)

    assert first == second
    assert sim_tools.write_effect_count("create_payment_link") == 1


@pytest.mark.asyncio
async def test_failure_injection_simulate_error_arg(sim_tools):
    with pytest.raises(ToolInvocationError):
        await sim_tools.invoke(
            "get_balance",
            {"borrower_id": B_DUE, "__simulate_error": True},
            "default",
        )


@pytest.mark.asyncio
async def test_action_failure_sets_tool_error_no_crash(sim_tools):
    registry = ActionRegistry(sim_tools)
    state = new_conversation_state("c-fail", "default", B_VULNERABLE)
    state.slots["amount_due"] = 8000

    updated = await registry.run_async("create_payment_link", state)

    assert updated.slots["tool_failed"] is True
    assert updated.slots["tool_error"]
    assert updated.slots["transfer_to_human"] is True


def test_executor_handoff_on_tool_failure_no_crash(sim_tools):
    runner = make_action_runner(sim_tools)
    state = new_conversation_state("c-pay", "default", B_VULNERABLE)
    state.slots["amount_due"] = 8000
    state = apply(state, [Command(command="start_flow", flow="pay_now")])

    result = run(state, FLOWS, runner)

    assert "create_payment_link" in result.actions_called
    assert result.state.slots["tool_failed"] is True
    assert result.transfer_to_human is True


@pytest.mark.asyncio
async def test_healthz_tools_simulate_mode(monkeypatch):
    monkeypatch.setenv("TOOLS_MODE", "simulate")
    monkeypatch.setenv("KB_STUB", "true")
    monkeypatch.setenv("LLM_STUB", "true")
    from app.config import get_settings

    get_settings.cache_clear()

    from httpx import ASGITransport, AsyncClient

    from app.main import app, lifespan

    async with lifespan(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/healthz")
    body = response.json()
    assert body["tools_mode"] == "simulate"
    assert body["client_modes"]["tools"] == "simulate"
