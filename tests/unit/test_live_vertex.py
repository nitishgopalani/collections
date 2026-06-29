import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from app.config import get_settings
from app.engine.command_gen import generate
from app.engine.tracker import new_conversation_state

PROMISE_FLOW = {
    "name": "promise_to_pay",
    "description": "Borrower agrees to pay on a future date (kal, parso, next week).",
    "score": 0.92,
}
DISPUTE_FLOW = {
    "name": "dispute",
    "description": "Borrower disputes the loan, amount, or prior payments.",
    "score": 0.85,
}


def _live_vertex_enabled() -> bool:
    load_dotenv()
    get_settings.cache_clear()
    settings = get_settings()
    creds = Path(settings.google_application_credentials)
    return not settings.llm_stub_mode and settings.gcp_project_id and creds.is_file()


@pytest.fixture
async def live_llm_client():
    load_dotenv()
    os.environ["LLM_STUB"] = "false"
    os.environ.setdefault("KB_STUB", "true")
    os.environ.setdefault("TOOLS_MODE", "simulate")
    get_settings.cache_clear()

    from app.clients.llm_vertex import create_llm_client

    client = create_llm_client()
    if client.is_stub:
        pytest.skip("Vertex live mode not configured (LLM_STUB=false + GCP creds)")
    if not await client.health():
        pytest.skip("Vertex API not reachable")
    yield client
    get_settings.cache_clear()


def _state(today: str = "2026-06-25"):
    state = new_conversation_state("live-cmd", "default", "live-borrower")
    state.slots["call_date"] = today
    return state


@pytest.mark.live_vertex
@pytest.mark.asyncio
async def test_live_kal_paisa_de_dunga_ptp(live_llm_client):
    state = _state()
    result = await generate(
        "kal paisa de dunga",
        state,
        [PROMISE_FLOW, DISPUTE_FLOW],
        llm=live_llm_client,
    )
    commands = result.commands
    types = [cmd.command for cmd in commands]
    assert "start_flow" in types
    ptp_flows = [cmd.flow for cmd in commands if cmd.command == "start_flow"]
    assert "promise_to_pay" in ptp_flows
    ptp_dates = [
        cmd.value for cmd in commands if cmd.command == "set_slot" and cmd.name == "ptp_date"
    ]
    if ptp_dates:
        assert str(ptp_dates[0]).startswith("2026-")


@pytest.mark.live_vertex
@pytest.mark.asyncio
async def test_live_multi_signal_dispute_and_ptp(live_llm_client):
    state = _state()
    result = await generate(
        "maine pay kar diya par parso dekhunga",
        state,
        [DISPUTE_FLOW, PROMISE_FLOW],
        llm=live_llm_client,
    )
    starts = [cmd.flow for cmd in result.commands if cmd.command == "start_flow"]
    assert "dispute" in starts
    assert "promise_to_pay" in starts


@pytest.mark.live_vertex
@pytest.mark.asyncio
async def test_live_healthz_llm_live_kb_tools_stub():
    load_dotenv()
    os.environ["LLM_STUB"] = "false"
    os.environ["KB_STUB"] = "true"
    os.environ["TOOLS_MODE"] = "simulate"
    get_settings.cache_clear()

    from httpx import ASGITransport, AsyncClient

    from app.main import app, lifespan

    async with lifespan(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/healthz")
    body = response.json()
    assert body["llm_stub_mode"] is False
    assert body["kb_stub_mode"] is True
    assert body["tools_mode"] == "simulate"
    assert body["client_modes"]["llm"] == "live"
    assert body["client_modes"]["kb"] == "stub"
    assert body["client_modes"]["tools"] == "simulate"


@pytest.mark.skipif(not _live_vertex_enabled(), reason="LLM_STUB=false + vertex-sa.json required")
@pytest.mark.live_vertex
def test_live_vertex_config_gate():
    assert _live_vertex_enabled()
