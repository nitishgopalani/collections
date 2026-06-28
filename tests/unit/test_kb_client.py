from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.clients.kb import HEALTH_API_PATH, KBClientImpl, create_kb_client
from app.config import get_settings


@pytest.mark.asyncio
async def test_kb_search_fail_soft_returns_empty(monkeypatch):
    monkeypatch.setenv("KB_STUB", "false")
    monkeypatch.setenv("KB_API_KEY", "test-key")
    get_settings.cache_clear()
    client = create_kb_client()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.post.side_effect = httpx.TimeoutException("timeout")

        results = await client.search("kal payment")
        assert results == []


@pytest.mark.asyncio
async def test_kb_search_401_fail_soft(monkeypatch):
    monkeypatch.setenv("KB_STUB", "false")
    monkeypatch.setenv("KB_API_KEY", "bad-key")
    monkeypatch.setenv("KB_SEARCH_PATH", "/search")
    get_settings.cache_clear()
    client = create_kb_client()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        response = httpx.Response(
            401, request=httpx.Request("POST", "https://api.fonada.ai/search")
        )
        mock_client.post.return_value = response

        results = await client.search("kal payment")
        assert results == []


@pytest.mark.asyncio
async def test_kb_search_posts_real_contract(monkeypatch):
    monkeypatch.setenv("KB_STUB", "false")
    monkeypatch.setenv("KB_API_KEY", "client-key-uuid")
    monkeypatch.setenv("KB_USER_AGENT", "Supabase-Function/1.0")
    monkeypatch.setenv("KB_SEARCH_PATH", "/search")
    monkeypatch.setenv("KB_TOP_K", "10")
    get_settings.cache_clear()
    client = create_kb_client()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.post.return_value = httpx.Response(
            200,
            json={
                "results": [
                    {"id": "42", "text": "[[flow:ptp]] preview", "score": 0.77},
                ]
            },
            request=httpx.Request("POST", "https://api.fonada.ai/search"),
        )

        results = await client.search("kal payment")
        assert results == [
            {"doc_id": "42", "score": 0.77, "text": "[[flow:ptp]] preview"},
        ]

        call_kwargs = mock_client.post.call_args.kwargs
        assert call_kwargs["headers"]["X-API-Key"] == "client-key-uuid"
        assert call_kwargs["headers"]["User-Agent"] == "Supabase-Function/1.0"
        assert call_kwargs["json"] == {"query": "kal payment", "top_k": 10}
        assert mock_client.post.call_args.args[0] == "https://api.fonada.ai/search"


@pytest.mark.asyncio
async def test_kb_health_uses_api_health(monkeypatch):
    monkeypatch.setenv("KB_STUB", "false")
    get_settings.cache_clear()
    client = create_kb_client()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = httpx.Response(
            200,
            request=httpx.Request("GET", "https://api.fonada.ai/api/health"),
        )

        ok = await client.health()
        assert ok is True
        url = mock_client.get.call_args.args[0]
        assert url.endswith(HEALTH_API_PATH)
        headers = mock_client.get.call_args.kwargs["headers"]
        assert headers["X-API-Key"] == "health-check"


@pytest.mark.asyncio
async def test_kb_stub_returns_empty():
    get_settings.cache_clear()
    client = create_kb_client()
    assert client.is_stub is True
    assert await client.search("hello") == []


def test_kb_parse_results_shape():
    client = KBClientImpl()
    parsed = client._parse_results(
        {
            "results": [
                {
                    "doc_id": "d1",
                    "combined_score": 0.88,
                    "text_preview": "[[flow:ptp]] text",
                }
            ]
        }
    )
    assert parsed == [{"doc_id": "d1", "score": 0.88, "text": "[[flow:ptp]] text"}]


def test_kb_parse_distance_to_score():
    client = KBClientImpl()
    parsed = client._parse_results(
        {
            "results": [
                {
                    "rank": 1,
                    "text": "[[flow:promise_to_pay]] kal",
                    "distance": 1.29,
                }
            ]
        }
    )
    assert parsed[0]["text"].startswith("[[flow:promise_to_pay]]")
    assert parsed[0]["score"] == 1.0 / (1.0 + 1.29)
