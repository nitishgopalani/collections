"""Groq LLM client (OpenAI-compatible chat completions over httpx).

Groq's LPU gives sub-100ms TTFT — ideal for the narrow "classify utterance ->
commands" job. Same interface as VertexLLMClient so it's a drop-in via
create_llm_client() when LLM_PROVIDER=groq.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings
from app.memory.pii import mask_pii_in_value

logger = logging.getLogger(__name__)

_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


class GroqLLMClient:
    """Gemini-compatible interface backed by Groq's OpenAI-style API."""

    def __init__(self, timeout: float = 20.0) -> None:
        self._settings = get_settings()
        self._timeout = timeout

    @property
    def is_stub(self) -> bool:
        return self._settings.llm_stub_mode or not self._settings.groq_api_key

    async def ping(self) -> bool:
        return await self.health()

    async def health(self) -> bool:
        return not self.is_stub or self._settings.llm_stub_mode

    async def complete(
        self,
        system: str,
        user: str,
        *,
        json_only: bool = True,
        response_schema: Any | None = None,
    ) -> str:
        if self.is_stub:
            return "[]"
        # response_schema is Gemini-shaped; on Groq we rely on JSON mode + the
        # prompt's slot hints + the engine validator. Keep the param for interface parity.
        _ = response_schema
        sys_prompt = system
        if json_only:
            sys_prompt = system + ' Return a JSON object: {"commands": [ ...the array... ]}.'
        body: dict[str, Any] = {
            "model": self._settings.groq_model,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
        }
        if json_only:
            body["response_format"] = {"type": "json_object"}
        url = self._settings.groq_base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self._settings.groq_api_key}"}

        for attempt in (1, 2):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(url, json=body, headers=headers)
                if resp.status_code in _TRANSIENT_STATUS and attempt == 1:
                    logger.warning("Groq transient %s, retrying once", resp.status_code)
                    continue
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"] or ""
                return content.strip()
            except httpx.HTTPError as exc:
                if attempt == 1:
                    logger.warning("Groq request error, retrying once: %s", mask_pii_in_value(str(exc)))
                    continue
                raise
        return "[]"


def create_groq_client() -> GroqLLMClient:
    return GroqLLMClient()
