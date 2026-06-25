import logging
from typing import Any

import httpx

from app.config import get_settings
from app.memory.pii import mask_pii_in_value

logger = logging.getLogger(__name__)


class UpstashRestClient:
    """Minimal Upstash Redis REST client (command-array protocol)."""

    def __init__(self, timeout: float = 10.0) -> None:
        settings = get_settings()
        self._url = settings.upstash_redis_rest_url.rstrip("/")
        self._token = settings.upstash_redis_rest_token
        self._timeout = timeout

    async def ping(self) -> bool:
        try:
            result = await self.execute(["PING"])
            return bool(result == "PONG")
        except Exception:
            logger.warning("Upstash PING failed")
            return False

    async def execute(self, command: list[Any]) -> Any:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                self._url,
                headers={"Authorization": f"Bearer {self._token}"},
                json=command,
            )
            response.raise_for_status()
            payload = response.json()
            if "error" in payload:
                raise RuntimeError(payload["error"])
            return payload.get("result")

    async def get(self, key: str) -> str | None:
        result = await self.execute(["GET", key])
        if result is None:
            return None
        return str(result)

    async def set(self, key: str, value: str, *, ttl_seconds: int | None = None) -> None:
        if ttl_seconds is not None:
            await self.execute(["SET", key, value, "EX", ttl_seconds])
        else:
            await self.execute(["SET", key, value])

    async def rpush(self, key: str, value: str) -> None:
        await self.execute(["RPUSH", key, value])

    async def lrange(self, key: str, start: int = 0, end: int = -1) -> list[str]:
        result = await self.execute(["LRANGE", key, str(start), str(end)])
        if not result:
            return []
        return [str(item) for item in result]

    def log_command(self, command: list[Any]) -> None:
        logger.debug("upstash command=%s", mask_pii_in_value(command))
