import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.flows.loader import FLOWS_DIR, load_all_flows

logger = logging.getLogger(__name__)

FLOW_TAG_PATTERN = re.compile(r"\[\[flow:([a-zA-Z0-9_]+)\]\]")
FLOW_DOC_MAP_PATH = FLOWS_DIR / "flow_doc_map.json"


class FlowCandidate(BaseModel):
    name: str
    description: str
    score: float = 0.0


_RETRIEVAL_CACHE_TTL_SECONDS = 300.0
_RETRIEVAL_CACHE_MAX_ENTRIES = 128
_retrieval_cache: dict[str, tuple[float, list[FlowCandidate]]] = {}


def _retrieval_cache_key(tenant_id: str, text: str) -> str:
    normalized = " ".join(text.lower().split())
    digest = hashlib.sha256(f"{tenant_id}:{normalized}".encode()).hexdigest()
    return digest


def clear_retrieval_cache() -> None:
    """Test helper — flush cached KB retrieval results."""
    _retrieval_cache.clear()


def _cache_get(key: str) -> list[FlowCandidate] | None:
    entry = _retrieval_cache.get(key)
    if entry is None:
        return None
    ts, candidates = entry
    if time.monotonic() - ts > _RETRIEVAL_CACHE_TTL_SECONDS:
        _retrieval_cache.pop(key, None)
        return None
    return candidates


def _cache_put(key: str, candidates: list[FlowCandidate]) -> None:
    if len(_retrieval_cache) >= _RETRIEVAL_CACHE_MAX_ENTRIES:
        oldest_key = min(_retrieval_cache, key=lambda k: _retrieval_cache[k][0])
        _retrieval_cache.pop(oldest_key, None)
    _retrieval_cache[key] = (time.monotonic(), candidates)


def flow_doc_map_path() -> Path:
    return FLOW_DOC_MAP_PATH


def load_flow_doc_map(path: Path | None = None) -> dict[str, str]:
    """doc_id → flow_name map written at KB seed time."""
    map_path = path or FLOW_DOC_MAP_PATH
    if not map_path.is_file():
        return {}
    data = json.loads(map_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    return {str(doc_id): str(flow_name) for doc_id, flow_name in data.items()}


def parse_flow_tag(text: str) -> str | None:
    match = FLOW_TAG_PATTERN.search(text)
    if match:
        return match.group(1)
    return None


def tagged_flow_text(flow_name: str, description: str) -> str:
    return f"[[flow:{flow_name}]] {description.strip()}"


def resolve_flows(
    results: list[dict[str, Any]],
    *,
    doc_map: dict[str, str] | None = None,
    flows_dir: Path | None = None,
) -> list[FlowCandidate]:
    """Map KB text chunks to local flow names. Drops unresolvable chunks."""
    doc_map = doc_map if doc_map is not None else load_flow_doc_map()
    flow_set = load_all_flows(flows_dir or FLOWS_DIR)

    best: dict[str, FlowCandidate] = {}

    for item in results:
        doc_id = str(item.get("doc_id", ""))
        text = str(item.get("text", ""))
        score = float(item.get("score", 0.0))

        flow_name = parse_flow_tag(text)
        if flow_name is None and doc_id:
            flow_name = doc_map.get(doc_id)

        if not flow_name:
            logger.debug("KB chunk dropped: unresolvable doc_id=%s", doc_id)
            continue

        local_flow = flow_set.flows.get(flow_name)
        description = local_flow.description if local_flow else text
        tag_stripped = FLOW_TAG_PATTERN.sub("", text).strip()
        if local_flow is None and tag_stripped:
            description = tag_stripped

        candidate = FlowCandidate(name=flow_name, description=description, score=score)
        existing = best.get(flow_name)
        if existing is None or candidate.score > existing.score:
            best[flow_name] = candidate

    return sorted(best.values(), key=lambda candidate: candidate.score, reverse=True)


async def retrieve_flow_candidates(
    kb_client: Any,
    text: str,
    tenant_id: str,
    k: int = 6,
    *,
    use_cache: bool = True,
) -> list[FlowCandidate]:
    """Search KB and resolve chunks to local flow candidates."""
    cache_key = _retrieval_cache_key(tenant_id, text)
    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    raw = await kb_client.retrieve(text, tenant_id, k=k)
    candidates = resolve_flows(raw)
    if use_cache:
        _cache_put(cache_key, candidates)
    return candidates
