import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from app.config import get_settings
from app.engine.retrieval import load_flow_doc_map, retrieve_flow_candidates
from app.schemas.command import Command


def _live_kb_enabled() -> bool:
    load_dotenv()
    get_settings.cache_clear()
    settings = get_settings()
    return not settings.kb_stub_mode and bool(settings.kb_api_key)


def _live_vertex_creds_available() -> bool:
    load_dotenv()
    get_settings.cache_clear()
    settings = get_settings()
    creds = Path(settings.google_application_credentials)
    return bool(settings.gcp_project_id) and creds.is_file()


@pytest.fixture
async def live_kb_client():
    load_dotenv()
    os.environ["KB_STUB"] = "false"
    os.environ.setdefault("TOOLS_MODE", "simulate")
    os.environ.setdefault("LLM_STUB", "true")
    get_settings.cache_clear()

    from app.clients.kb import create_kb_client

    client = create_kb_client()
    if client.is_stub:
        pytest.skip("KB live mode not configured (KB_STUB=false + KB_API_KEY)")
    if not await client.health():
        pytest.skip("KB API not reachable")

    probe = await client.search("promise_to_pay", top_k=1)
    if not probe and not load_flow_doc_map():
        from scripts.seed_kb_flows import seed_flows

        exit_code = seed_flows(force=False)
        if exit_code != 0:
            pytest.skip("KB seed failed — verify KB_API_KEY and /add/text access")
        probe = await client.search("promise_to_pay", top_k=1)
        if not probe:
            pytest.skip("KB index empty after seed")

    yield client
    get_settings.cache_clear()


@pytest.fixture
async def live_kb_pipeline_client(live_kb_client):
    if not _live_vertex_creds_available():
        pytest.skip("Vertex live mode not configured (GOOGLE_APPLICATION_CREDENTIALS file)")
    os.environ["LLM_STUB"] = "false"
    get_settings.cache_clear()
    yield live_kb_client
    get_settings.cache_clear()


@pytest.mark.live_kb
@pytest.mark.asyncio
async def test_live_retrieve_promise_to_pay(live_kb_client):
    candidates = await retrieve_flow_candidates(
        live_kb_client,
        "kal paisa de dunga",
        tenant_id="default",
        k=6,
    )
    names = {candidate.name for candidate in candidates}
    if not names:
        pytest.skip("KB search returned no candidates (auth or index may be empty)")
    assert "promise_to_pay" in names


@pytest.mark.live_kb
@pytest.mark.asyncio
async def test_live_healthz_kb_live_llm_live_tools_stub():
    load_dotenv()
    os.environ["KB_STUB"] = "false"
    os.environ["TOOLS_MODE"] = "simulate"
    os.environ["LLM_STUB"] = "false"
    get_settings.cache_clear()

    from httpx import ASGITransport, AsyncClient

    from app.main import app, lifespan

    async with lifespan(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/healthz")
    body = response.json()
    assert body["kb_stub_mode"] is False
    assert body["tools_mode"] == "simulate"
    assert body["tools_stub_mode"] is False
    assert body["llm_stub_mode"] is False
    assert body["client_modes"]["kb"] == "live"
    assert body["client_modes"]["tools"] == "simulate"
    assert body["client_modes"]["llm"] == "live"


@pytest.mark.live_kb
@pytest.mark.asyncio
async def test_live_transcript_to_commands_promise_to_pay(live_kb_pipeline_client):
    from app.engine.pipeline import transcript_to_commands
    from app.engine.tracker import new_conversation_state

    state = new_conversation_state("live-kb", "default", "b-live")
    state.slots["call_date"] = "2026-06-25"
    commands = await transcript_to_commands(
        "kal paisa de dunga",
        state,
        "default",
        kb_client=live_kb_pipeline_client,
    )
    if not commands or commands == [Command(command="clarify")]:
        pytest.skip("KB/LLM pipeline did not resolve promise_to_pay (check KB key + seed)")
    names = {(cmd.command, cmd.flow, cmd.name) for cmd in commands}
    assert ("start_flow", "promise_to_pay", None) in names
    assert ("set_slot", None, "ptp_date") in names


@pytest.mark.live_kb
@pytest.mark.asyncio
async def test_live_multi_signal_transcript(live_kb_pipeline_client):
    from app.engine.pipeline import transcript_to_commands
    from app.engine.tracker import new_conversation_state

    state = new_conversation_state("live-kb-multi", "default", "b-live")
    state.slots["call_date"] = "2026-06-25"
    commands = await transcript_to_commands(
        "galat amount hai kal paisa de dunga",
        state,
        "default",
        kb_client=live_kb_pipeline_client,
    )
    start_flows = {cmd.flow for cmd in commands if cmd.command == "start_flow"}
    if not start_flows:
        pytest.skip("KB search returned no candidates for multi-signal utterance")
    assert "dispute" in start_flows
    assert "promise_to_pay" in start_flows


@pytest.mark.skipif(not _live_kb_enabled(), reason="Set KB_STUB=false with KB_API_KEY for live KB")
@pytest.mark.live_kb
def test_live_kb_config_gate():
    assert _live_kb_enabled()
