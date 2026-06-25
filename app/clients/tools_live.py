import logging
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

INVOKE_PATH = "/invoke"

# DECISION NEEDED: confirm real tool contract + MPLS access before TOOLS_MODE=live.


class LiveToolClient:
    """HTTP client for the governed tool endpoint (TOOLS_MODE=live)."""

    def __init__(self, timeout: float = 15.0) -> None:
        self._settings = get_settings()
        self._timeout = timeout

    @property
    def is_stub(self) -> bool:
        return False

    @property
    def mode(self) -> str:
        return "live"

    async def ping(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(self._settings.tools_url)
                return response.status_code < 500
        except httpx.HTTPError:
            logger.warning("Tools ping failed")
            return False

    async def invoke(
        self,
        tool: str,
        args: dict[str, Any],
        tenant_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool": tool,
            "args": args,
            "tenant_id": tenant_id,
        }
        if idempotency_key is not None:
            payload["idempotency_key"] = idempotency_key
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._settings.tools_url.rstrip('/')}{INVOKE_PATH}",
                json=payload,
            )
            response.raise_for_status()
            result: dict[str, Any] = response.json()
            return result
