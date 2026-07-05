import asyncio
import logging
import os
import threading
from collections.abc import AsyncIterator, Iterator
from typing import Any

from app.config import get_settings
from app.memory.pii import mask_pii_in_value

logger = logging.getLogger(__name__)

_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


def _stream_piece_text(piece: Any) -> str:
    """Extract the text delta from one streamed response, tolerating empty parts."""
    try:
        return piece.text or ""
    except Exception:
        # Streamed responses without text parts (e.g. finish-reason-only tail).
        return ""


def _close_stream_quietly(stream: Any) -> None:
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


class VertexLLMClient:
    """Gemini via Vertex AI (google-genai SDK). Model id swappable via GEMINI_MODEL_ID.

    Uses the google-genai SDK (``genai.Client(vertexai=True, ...)``) rather than
    the legacy ``vertexai.generative_models`` SDK because live voice turns need
    thinking control (GEMINI_THINKING_LEVEL) — thinking silently adds hundreds
    of ms of TTFT and the legacy SDK cannot configure it.
    """

    def __init__(self, timeout: float = 30.0) -> None:
        self._settings = get_settings()
        self._timeout = timeout
        self._client: Any = None
        self._client_lock = threading.Lock()

    @property
    def is_stub(self) -> bool:
        return self._settings.llm_stub_mode

    def _ensure_credentials_env(self) -> None:
        creds = self._settings.google_application_credentials
        if creds:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds

    def _genai_client(self) -> Any:
        """Lazily construct the google-genai Vertex client (thread-safe)."""
        with self._client_lock:
            if self._client is None:
                self._ensure_credentials_env()
                from google import genai

                self._client = genai.Client(
                    vertexai=True,
                    project=self._settings.gcp_project_id,
                    location=self._settings.gcp_region,
                )
            return self._client

    def _thinking_config(self) -> Any | None:
        """Thinking control for live turns, from GEMINI_THINKING_LEVEL.

        Gemini 3.x takes the thinking_level enum (minimal|low|medium|high; it
        cannot be fully disabled — minimal is the floor). Gemini 2.5 takes the
        numeric thinking_budget; minimal/off/0/none maps to budget 0 (disabled).
        Empty setting: no thinking config sent (model default).
        """
        level = (self._settings.gemini_thinking_level or "").strip().lower()
        if not level:
            return None
        from google.genai import types

        model = (self._settings.gemini_model_id or "").lower()
        if model.startswith("gemini-3"):
            if level in ("off", "0", "none", "disable", "disabled"):
                level = "minimal"  # 3.x floor: thinking cannot be disabled
            return types.ThinkingConfig(thinking_level=level)
        # 2.5-era models use the numeric budget; 0 disables thinking entirely.
        if level in ("minimal", "off", "0", "none", "disable", "disabled"):
            return types.ThinkingConfig(thinking_budget=0)
        return None

    def _generation_config(
        self,
        *,
        json_only: bool = False,
        response_schema: Any | None = None,
        max_output_tokens: int | None = None,
        include_thinking: bool = True,
    ) -> Any:
        from google.genai import types

        kwargs: dict[str, Any] = {"temperature": 0.1}
        if json_only:
            kwargs["response_mime_type"] = "application/json"
            if response_schema is not None:
                kwargs["response_schema"] = response_schema
        if max_output_tokens is not None:
            kwargs["max_output_tokens"] = max_output_tokens
        if include_thinking:
            thinking = self._thinking_config()
            if thinking is not None:
                kwargs["thinking_config"] = thinking
        return types.GenerateContentConfig(**kwargs)

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
        client = self._genai_client()
        client.models.generate_content(
            model=self._settings.gemini_model_id,
            contents="ping",
            config=self._generation_config(max_output_tokens=1, include_thinking=False),
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

    async def stream(self, system: str, user: str) -> AsyncIterator[str]:
        """Stream plain-text token deltas from Gemini (streamGenerateContent).

        Additive next to :meth:`complete` — existing callers are unchanged.
        Used by prompt-mode turns (free text, no JSON constraint). The blocking
        SDK iterator runs on a worker thread; closing this async generator
        (e.g. barge-in cancel) signals the worker to stop and close the
        underlying stream, so nothing leaks.
        """
        if self.is_stub:
            return
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        stop = threading.Event()

        def _put(item: tuple[str, Any]) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:
                # Event loop already closed — consumer is gone, drop the item.
                pass

        def _produce() -> None:
            sdk_stream: Iterator[Any] | None = None
            try:
                sdk_stream = self._start_stream_sync(system, user)
                for piece in sdk_stream:
                    if stop.is_set():
                        break
                    text = _stream_piece_text(piece)
                    if text:
                        _put(("token", text))
                _put(("end", None))
            except Exception as exc:  # surfaced to the consumer below
                _put(("error", exc))
            finally:
                if sdk_stream is not None:
                    _close_stream_quietly(sdk_stream)

        worker = threading.Thread(target=_produce, name="vertex-llm-stream", daemon=True)
        worker.start()
        try:
            while True:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=self._timeout)
                if kind == "token":
                    yield payload
                elif kind == "end":
                    return
                else:
                    raise payload
        finally:
            # Normal exhaustion, consumer cancel (aclose), or error: stop the
            # worker so the SDK stream is closed and the thread exits.
            stop.set()

    def _start_stream_sync(self, system: str, user: str) -> Iterator[Any]:
        client = self._genai_client()
        prompt = f"SYSTEM:\n{system}\n\nUSER:\n{user}"
        return client.models.generate_content_stream(
            model=self._settings.gemini_model_id,
            contents=prompt,
            config=self._generation_config(),
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
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return True
        code = getattr(exc, "code", None)
        if isinstance(code, int) and code in _TRANSIENT_STATUS_CODES:
            return True
        try:
            from google.api_core import exceptions as gcp_exceptions

            if isinstance(exc, gcp_exceptions.GoogleAPICallError):
                return exc.code in _TRANSIENT_STATUS_CODES
        except ImportError:
            pass
        return False

    def _complete_sync(
        self,
        system: str,
        user: str,
        json_only: bool,
        response_schema: Any | None = None,
    ) -> str:
        client = self._genai_client()
        prompt = f"SYSTEM:\n{system}\n\nUSER:\n{user}"
        try:
            response = client.models.generate_content(
                model=self._settings.gemini_model_id,
                contents=prompt,
                config=self._generation_config(
                    json_only=json_only, response_schema=response_schema
                ),
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
            response = client.models.generate_content(
                model=self._settings.gemini_model_id,
                contents=prompt,
                config=self._generation_config(json_only=json_only, response_schema=None),
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
