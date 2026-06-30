import asyncio
import logging
import os
from typing import Any

from app.config import get_settings
from app.memory.pii import mask_pii_in_value

logger = logging.getLogger(__name__)

_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


class VertexLLMClient:
    """Gemini via Vertex AI. Model id swappable via GEMINI_MODEL_ID env."""

    def __init__(self, timeout: float = 30.0) -> None:
        self._settings = get_settings()
        self._timeout = timeout

    @property
    def is_stub(self) -> bool:
        return self._settings.llm_stub_mode

    def _ensure_credentials_env(self) -> None:
        creds = self._settings.google_application_credentials
        if creds:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds

    async def health(self) -> bool:
        if self.is_stub:
            return True
        if not self._settings.gcp_project_id:
            return False
        try:
            await asyncio.wait_for(asyncio.to_thread(self._health_sync), timeout=self._timeout)
            return True
        except Exception as exc:
            logger.warning("Vertex health check failed: %s", mask_pii_in_value(str(exc)))
            return False

    async def ping(self) -> bool:
        return await self.health()

    def _health_sync(self) -> None:
        self._ensure_credentials_env()
        import vertexai
        from vertexai.generative_models import GenerationConfig, GenerativeModel

        vertexai.init(
            project=self._settings.gcp_project_id,
            location=self._settings.gcp_region,
        )
        model = GenerativeModel(self._settings.gemini_model_id)
        model.generate_content(
            "ping",
            generation_config=GenerationConfig(
                temperature=0.0,
                max_output_tokens=1,
            ),
        )

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
        return await asyncio.wait_for(
            self._complete_with_retry(
                system, user, json_only=json_only, response_schema=response_schema
            ),
            timeout=self._timeout,
        )

    async def _complete_with_retry(
        self, system: str, user: str, *, json_only: bool, response_schema: Any | None = None
    ) -> str:
        try:
            return await asyncio.to_thread(
                self._complete_sync, system, user, json_only, response_schema
            )
        except Exception as exc:
            if not self._is_transient(exc):
                raise
            logger.warning("Vertex transient error, retrying once: %s", mask_pii_in_value(str(exc)))
            return await asyncio.to_thread(
                self._complete_sync, system, user, json_only, response_schema
            )

    def _is_transient(self, exc: Exception) -> bool:
        from google.api_core import exceptions as gcp_exceptions

        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return True
        if isinstance(exc, gcp_exceptions.GoogleAPICallError):
            return exc.code in _TRANSIENT_STATUS_CODES
        return False

    def _complete_sync(
        self,
        system: str,
        user: str,
        json_only: bool,
        response_schema: Any | None = None,
    ) -> str:
        self._ensure_credentials_env()
        import vertexai
        from vertexai.generative_models import GenerationConfig, GenerativeModel

        vertexai.init(
            project=self._settings.gcp_project_id,
            location=self._settings.gcp_region,
        )
        model = GenerativeModel(self._settings.gemini_model_id)
        generation_kwargs: dict[str, Any] = {"temperature": 0.1}
        if json_only:
            generation_kwargs["response_mime_type"] = "application/json"
            if response_schema is not None:
                generation_kwargs["response_schema"] = response_schema
        prompt = f"SYSTEM:\n{system}\n\nUSER:\n{user}"
        try:
            response = model.generate_content(
                prompt,
                generation_config=GenerationConfig(**generation_kwargs),
            )
        except Exception as exc:
            # Constrained-output schema unsupported/rejected by the SDK or model:
            # fall back to plain JSON so a turn never hard-fails over the schema.
            if response_schema is None:
                raise
            logger.warning(
                "Vertex response_schema rejected, retrying without schema: %s",
                mask_pii_in_value(str(exc)),
            )
            generation_kwargs.pop("response_schema", None)
            response = model.generate_content(
                prompt,
                generation_config=GenerationConfig(**generation_kwargs),
            )
        text = response.text or ""
        return text.strip()


def create_llm_client() -> Any:
    """Return the configured LLM client: Groq (fast LPU) or Vertex/Gemini (default)."""
    settings = get_settings()
    if (settings.llm_provider or "vertex").lower() == "groq":
        from app.clients.llm_groq import GroqLLMClient

        return GroqLLMClient()
    return VertexLLMClient()
