"""HTTP client for TOOLS_MODE=live — contract endpoints, 2s timeout, one retry, degrade."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.clients.tools_contract import (
    BORROWER_STATE_PATH,
    DISPOSITION_PATH,
    DISPOSITION_TOOLS,
    STATE_TOOLS,
    unwrap_state_payload,
)
from app.config import get_settings

logger = logging.getLogger(__name__)

INVOKE_PATH = "/invoke"
DEFAULT_TIMEOUT_S = 2.0


class LiveToolClient:
    """HTTP client for the tenant tools API (TOOLS_MODE=live)."""

    def __init__(
        self,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        tools_url: str | None = None,
    ) -> None:
        self._settings = get_settings()
        self._timeout = float(
            timeout if timeout is not None else getattr(self._settings, "tools_timeout_s", DEFAULT_TIMEOUT_S)
        )
        self._transport = transport
        self._tools_url = (tools_url or self._settings.tools_url or "").rstrip("/")
        self.last_call_ms: float = 0.0
        self.last_degraded: bool = False

    @property
    def is_stub(self) -> bool:
        return False

    @property
    def mode(self) -> str:
        return "live"

    def _client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {"timeout": self._timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    async def ping(self) -> bool:
        if not self._tools_url:
            return False
        try:
            async with self._client() as client:
                response = await client.get(self._tools_url)
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
        if tool in STATE_TOOLS:
            return await self.get_borrower_state(
                loan_ref=str(args.get("loan_ref") or args.get("account_ref") or ""),
                phone=str(args.get("phone") or ""),
                borrower_id=str(args.get("borrower_id") or ""),
                tenant_id=tenant_id,
            )
        if tool in DISPOSITION_TOOLS:
            return await self.post_disposition(args, tenant_id=tenant_id)
        return await self._post_invoke(tool, args, tenant_id, idempotency_key)

    async def get_borrower_state(
        self,
        *,
        loan_ref: str = "",
        phone: str = "",
        borrower_id: str = "",
        tenant_id: str = "",
    ) -> dict[str, Any]:
        params: dict[str, str] = {}
        if loan_ref:
            params["loan_ref"] = loan_ref
        if phone:
            params["phone"] = phone
        if borrower_id:
            params["borrower_id"] = borrower_id
        if tenant_id:
            params["tenant_id"] = tenant_id
        url = f"{self._tools_url}{BORROWER_STATE_PATH}"
        raw = await self._request("GET", url, params=params)
        if raw.get("degraded"):
            return raw
        payload = unwrap_state_payload(raw)
        return {"ok": True, "degraded": False, "result": payload}

    async def post_disposition(
        self,
        record: dict[str, Any],
        *,
        tenant_id: str = "",
    ) -> dict[str, Any]:
        body = dict(record)
        if tenant_id and "tenant" not in body:
            body["tenant"] = tenant_id
        raw = await self._request("POST", f"{self._tools_url}{DISPOSITION_PATH}", json=body)
        if raw.get("degraded"):
            return raw
        return {"ok": True, "degraded": False, "result": raw.get("result", raw)}

    async def _post_invoke(
        self,
        tool: str,
        args: dict[str, Any],
        tenant_id: str,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"tool": tool, "args": args, "tenant_id": tenant_id}
        if idempotency_key is not None:
            payload["idempotency_key"] = idempotency_key
        return await self._request(
            "POST",
            f"{self._tools_url}{INVOKE_PATH}",
            json=payload,
        )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        started = time.perf_counter()
        for attempt in range(2):
            try:
                async with self._client() as client:
                    response = await client.request(method, url, params=params, json=json)
                    response.raise_for_status()
                    self.last_call_ms = (time.perf_counter() - started) * 1000.0
                    self.last_degraded = False
                    body = response.json()
                    return body if isinstance(body, dict) else {"result": body}
            except (httpx.TimeoutException, httpx.HTTPError, ValueError) as exc:
                last_error = exc
                logger.warning(
                    "tools_live %s %s attempt=%s failed: %s",
                    method,
                    url,
                    attempt + 1,
                    exc,
                )
        self.last_call_ms = (time.perf_counter() - started) * 1000.0
        self.last_degraded = True
        logger.warning(
            "tools_live degraded after retry method=%s url=%s error=%s call_ms=%.0f",
            method,
            url,
            last_error,
            self.last_call_ms,
        )
        return {"ok": False, "degraded": True, "result": {}}
