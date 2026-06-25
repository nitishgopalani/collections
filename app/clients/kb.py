import logging
from typing import Any

import httpx

from app.clients.kb_headers import kb_client_headers, kb_health_headers
from app.config import get_settings
from app.memory.pii import mask_pii_in_value

logger = logging.getLogger(__name__)

# SECURITY: engine search/seed uses a client/agent API key only — never the admin key.

HEALTH_API_PATH = "/api/health"
STATS_PATH = "/stats"
HYBRID_SEARCH_PATH = "/knowledge-base/search"
ADD_TEXT_PATH = "/add/text"


class KBClientImpl:
    """Fonada FAISS knowledge-base client (POST /search, GET /api/health)."""

    def __init__(self, timeout: float = 10.0) -> None:
        self._settings = get_settings()
        self._timeout = timeout

    @property
    def is_stub(self) -> bool:
        return self._settings.kb_stub_mode

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._timeout,
            verify=self._settings.kb_verify_ssl,
        )

    async def health(self) -> bool:
        if self.is_stub:
            return True
        try:
            async with self._client() as client:
                response = await client.get(
                    f"{self._settings.kb_base_url.rstrip('/')}{HEALTH_API_PATH}",
                    headers=kb_health_headers(self._settings),
                )
                return response.status_code < 500
        except Exception as exc:
            logger.warning("KB health check failed: %s", mask_pii_in_value(str(exc)))
            return False

    async def ping(self) -> bool:
        return await self.health()

    async def stats(self) -> dict[str, Any] | None:
        if self.is_stub or not self._settings.kb_api_key:
            return None
        try:
            async with self._client() as client:
                response = await client.get(
                    f"{self._settings.kb_base_url.rstrip('/')}{STATS_PATH}",
                    headers=kb_client_headers(self._settings),
                )
                response.raise_for_status()
                data = response.json()
                return data if isinstance(data, dict) else {"raw": data}
        except Exception as exc:
            logger.warning("KB stats failed: %s", mask_pii_in_value(str(exc)))
            return None

    async def search(self, query: str, top_k: int | None = None) -> list[dict[str, Any]]:
        if self.is_stub:
            return []
        return await self._search_live(query, top_k=top_k)

    async def retrieve(self, text: str, tenant_id: str, k: int = 6) -> list[dict[str, Any]]:
        _ = tenant_id
        return await self.search(text, top_k=k)

    async def _search_live(self, query: str, top_k: int | None = None) -> list[dict[str, Any]]:
        if not self._settings.kb_api_key:
            logger.warning("KB search skipped: KB_API_KEY not configured")
            return []

        limit = top_k if top_k is not None else self._settings.kb_top_k
        base = self._settings.kb_base_url.rstrip("/")
        search_url = f"{base}{self._settings.kb_search_path}"

        try:
            return await self._post_search(search_url, query, limit)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.warning("KB /search returned 404; trying legacy hybrid path")
                try:
                    legacy_url = f"{base}{HYBRID_SEARCH_PATH}"
                    return await self._post_search_legacy(legacy_url, query, limit)
                except Exception as legacy_exc:
                    logger.warning(
                        "KB legacy search failed: %s",
                        mask_pii_in_value(str(legacy_exc)),
                    )
                    return []
            logger.warning(
                "KB search failed (status %s): %s",
                exc.response.status_code,
                mask_pii_in_value(str(exc)),
            )
            return []
        except Exception as exc:
            logger.warning("KB search error: %s", mask_pii_in_value(str(exc)))
            return []

    async def _post_search(self, url: str, query: str, top_k: int) -> list[dict[str, Any]]:
        payload = {"query": query, "top_k": top_k}
        async with self._client() as client:
            response = await client.post(
                url,
                headers=kb_client_headers(self._settings),
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                return []
            return self._parse_results(data)

    async def _post_search_legacy(self, url: str, query: str, top_k: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "query": query,
            "top_k": top_k,
            "use_hybrid": True,
        }
        async with self._client() as client:
            response = await client.post(
                url,
                headers=kb_client_headers(self._settings),
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                return []
            return self._parse_results(data)

    def _parse_results(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        raw_results = data.get("results")
        if raw_results is None and isinstance(data.get("data"), dict):
            raw_results = data["data"].get("results")
        if not isinstance(raw_results, list):
            return []

        parsed: list[dict[str, Any]] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            doc_id = (
                item.get("doc_id")
                or item.get("id")
                or item.get("document_id")
                or (item.get("metadata", {}) or {}).get("doc_id")
            )
            text = (
                item.get("text")
                or item.get("text_preview")
                or item.get("content")
                or item.get("document")
                or ""
            )
            score = (
                item.get("score")
                or item.get("combined_score")
                or item.get("similarity")
            )
            if score is None and item.get("distance") is not None:
                distance = float(item["distance"])
                score = 1.0 / (1.0 + distance)
            if score is None:
                score = 0.0
            parsed.append(
                {
                    "doc_id": str(doc_id) if doc_id is not None else "",
                    "score": float(score) if score is not None else 0.0,
                    "text": str(text),
                }
            )
        return parsed


def create_kb_client() -> KBClientImpl:
    return KBClientImpl()
