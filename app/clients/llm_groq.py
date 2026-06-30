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

# Groq models that support strict structured outputs (constrained decoding that
# GUARANTEES schema-valid JSON — exact keys, valid enum slot/flow names).
# https://console.groq.com/docs/structured-outputs
_STRICT_SCHEMA_MODELS = frozenset(
    {"openai/gpt-oss-20b", "openai/gpt-oss-120b", "openai/gpt-oss-safeguard-20b"}
)


def _strict_schema_from_gemini(array_schema: Any) -> dict[str, Any] | None:
    """Convert the engine's array-shaped response_schema into a Groq strict
    JSON-Schema (root object, additionalProperties:false, every property required).

    Strict mode requires all properties be listed in `required`; optional fields
    are made nullable (type union with "null") so the model may omit them.
    """
    if not isinstance(array_schema, dict) or array_schema.get("type") != "array":
        return None
    items = array_schema.get("items")
    if not isinstance(items, dict):
        return None
    props = items.get("properties")
    if not isinstance(props, dict):
        return None
    required_now = set(items.get("required") or [])

    strict_props: dict[str, Any] = {}
    for key, spec in props.items():
        spec = dict(spec) if isinstance(spec, dict) else {"type": "string"}
        if key not in required_now:
            base_type = spec.get("type", "string")
            if isinstance(base_type, str):
                spec["type"] = [base_type, "null"]
            enum_vals = spec.get("enum")
            if isinstance(enum_vals, list) and None not in enum_vals:
                spec["enum"] = [*enum_vals, None]
        strict_props[key] = spec

    item_schema = {
        "type": "object",
        "properties": strict_props,
        "required": list(strict_props.keys()),
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"commands": {"type": "array", "items": item_schema}},
        "required": ["commands"],
        "additionalProperties": False,
    }


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
        model = self._settings.groq_model
        sys_prompt = system
        if json_only:
            sys_prompt = system + ' Return a JSON object: {"commands": [ ...the array... ]}.'

        # Prefer strict structured outputs on supported models — this forces exact
        # keys + valid enum slot/flow names (no "slot" vs "name" drift). Fall back
        # to plain JSON mode on a schema 400 or on non-strict models.
        strict_schema = _strict_schema_from_gemini(response_schema)
        use_strict = strict_schema is not None and model in _STRICT_SCHEMA_MODELS

        def _build_body(response_format: dict[str, Any] | None) -> dict[str, Any]:
            body: dict[str, Any] = {
                "model": model,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.1,
            }
            if response_format is not None:
                body["response_format"] = response_format
            return body

        json_object_fmt = {"type": "json_object"} if json_only else None
        if use_strict:
            response_format: dict[str, Any] | None = {
                "type": "json_schema",
                "json_schema": {
                    "name": "command_list",
                    "strict": True,
                    "schema": strict_schema,
                },
            }
        else:
            response_format = json_object_fmt

        url = self._settings.groq_base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self._settings.groq_api_key}"}

        for attempt in (1, 2):
            body = _build_body(response_format)
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(url, json=body, headers=headers)
                if resp.status_code in _TRANSIENT_STATUS and attempt == 1:
                    logger.warning("Groq transient %s, retrying once", resp.status_code)
                    continue
                # Schema rejected (400) — drop strict and retry once with JSON mode.
                if resp.status_code == 400 and response_format is not None and \
                        response_format.get("type") == "json_schema" and attempt == 1:
                    logger.warning("Groq json_schema rejected (400); falling back to json_object")
                    response_format = json_object_fmt
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
