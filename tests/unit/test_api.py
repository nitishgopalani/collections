import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app, lifespan
from tests.helpers.scripted_clients import ScriptedKB, ScriptedLLM


@pytest.fixture
async def async_client():
    async with lifespan(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.mark.asyncio
async def test_healthz_returns_200(async_client: AsyncClient):
    response = await async_client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["stub_mode"] is True
    assert body["memory_stub_mode"] is True
    assert body["kb_stub_mode"] is True
    assert body["tools_mode"] == "simulate"
    assert body["client_modes"]["tools"] == "simulate"
    assert body["tools_stub_mode"] is False
    assert all(body["clients"].values())


@pytest.mark.asyncio
async def test_turn_runs_full_pipeline(async_client: AsyncClient):
    """/turn uses handle_turn — clarify path when KB/LLM stubs return empty."""
    payload = {
        "call_id": "call-1",
        "tenant_id": "default",
        "borrower_id": "borrower-1",
        "transcript": "kal payment kar dunga",
        "turn_meta": {"call_date": "2026-06-25"},
    }
    response = await async_client.post("/turn", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["reply_text"]
    assert body["state_version"] == 1
    assert body["audit_id"]


@pytest.mark.asyncio
async def test_turn_increments_state_on_second_call(async_client: AsyncClient):
    borrower_id = "borrower-multi-turn"
    call_id = "call-multi-turn"
    payload = {
        "call_id": call_id,
        "tenant_id": "default",
        "borrower_id": borrower_id,
        "transcript": "hello",
        "turn_meta": {"call_date": "2026-06-25"},
    }
    first = await async_client.post("/turn", json=payload)
    second = await async_client.post("/turn", json=payload)
    assert first.json()["state_version"] == 1
    assert second.json()["state_version"] == 2


@pytest.mark.asyncio
async def test_turn_with_scripted_clients_via_app_state(async_client: AsyncClient):
    """Inject scripted KB/LLM on app state for deterministic pipeline output."""
    app.state.kb = ScriptedKB(
        [{"doc_id": "1", "score": 0.9, "text": "[[flow:promise_to_pay]] kal"}]
    )
    app.state.llm = ScriptedLLM(
        [
            [
                {"command": "start_flow", "flow": "promise_to_pay"},
                {"command": "set_slot", "name": "ptp_date", "value": "2026-06-27"},
            ]
        ]
    )

    payload = {
        "call_id": "call-scripted",
        "tenant_id": "default",
        "borrower_id": "borrower-scripted",
        "transcript": "kal de dunga",
        "turn_meta": {"call_date": "2026-06-25"},
    }
    from app.memory.store import InMemoryMemoryStore
    from app.schemas.state import BorrowerRecord

    memory = app.state.memory
    if isinstance(memory, InMemoryMemoryStore):
        await memory.save_borrower(
            BorrowerRecord(
                borrower_id="borrower-scripted",
                loan={"amount_due": 5000, "dpd": 45, "bucket": "30-60"},
                identity={"identity_ok": True},
            )
        )

    response = await async_client.post("/turn", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert "schedule_followup" in body["actions_executed"]
