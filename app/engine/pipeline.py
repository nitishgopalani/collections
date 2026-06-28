"""End-to-end Sprint 4 path: KB retrieval → command generation."""

from typing import Any

from app.clients.kb import create_kb_client
from app.clients.llm_vertex import create_llm_client
from app.engine.command_gen import generate
from app.engine.retrieval import retrieve_flow_candidates
from app.schemas.command import Command
from app.schemas.state import ConversationState


async def transcript_to_commands(
    transcript: str,
    state: ConversationState,
    tenant_id: str,
    *,
    kb_client: Any | None = None,
    llm_client: Any | None = None,
) -> list[Command]:
    """Live KB search → flow resolution → Gemini → validated commands."""
    kb = kb_client or create_kb_client()
    llm = llm_client or create_llm_client()

    candidates = await retrieve_flow_candidates(kb, transcript, tenant_id)
    candidate_payload = [
        {"name": c.name, "description": c.description, "score": c.score} for c in candidates
    ]
    return await generate(transcript, state, candidate_payload, llm=llm)
