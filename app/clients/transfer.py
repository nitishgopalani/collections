"""Live call-transfer provider (Model A: brain calls the transfer endpoint).

The *decision* to transfer is made by flow logic (any ``action: transfer_call``
step, after the flow has spoken a "connecting you to an agent" line). The
*mechanism* — bridging a live human onto the same call — is this module.

Set ``TRANSFER_MODE=stub`` (default) to just log intent and return ``pending``
(the flow still speaks the handoff line and ends the bot leg). Set
``TRANSFER_MODE=live`` + ``TRANSFER_ENDPOINT_URL`` + ``TRANSFER_API_KEY`` +
``TRANSFER_DEFAULT_TARGET`` and this code POSTs to the endpoint.

Contract (voip.ivrobd.com /v1/transfer): POST JSON
``{session_id, transferring_number, context, priority, delay_ms, environment,
call_type}`` with header ``X-API-Key``. ``session_id`` is the exact id the Go
server received from the dialer (we pass it through as ``call_id``), and
``transferring_number`` is the human agent to bridge.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

# Dispositions this module may stamp on the call.
DISP_PENDING = "TRANSFER_PENDING"
DISP_INITIATED = "TRANSFERRED"
DISP_FAILED = "TRANSFER_FAILED"


@dataclass
class TransferResult:
    """Outcome of a transfer attempt.

    status:
      - ``pending``   — endpoint not configured (stub); no live bridge happened.
      - ``initiated`` — endpoint accepted the request; human is being bridged.
      - ``failed``    — endpoint errored/timed out; caller should fall back.
    """

    status: str
    disposition: str
    detail: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("pending", "initiated")


def _build_payload(*, call_id: str, target: str, settings: Any) -> dict[str, Any]:
    """Build the voip.ivrobd.com /v1/transfer body.

    ``call_id`` is the WS ``session_id`` the Go server received from the dialer, so
    we echo it straight back as ``session_id`` (same id both ways). ``target`` is
    the human agent's ``transferring_number``.
    """
    return {
        "session_id": call_id,
        "transferring_number": target,
        "context": getattr(settings, "transfer_context", "transfer-gen") or "transfer-gen",
        "priority": int(getattr(settings, "transfer_priority", 1) or 1),
        "delay_ms": int(getattr(settings, "transfer_delay_ms", 4000) or 4000),
        "environment": getattr(settings, "transfer_environment", "prod") or "prod",
        "call_type": getattr(settings, "transfer_call_type", "outbound") or "outbound",
    }


async def initiate_transfer(*, call_id: str, target: str, reason: str) -> TransferResult:
    """Attempt a live transfer. Never raises — failures return a ``failed`` result."""
    settings = get_settings()
    mode = (getattr(settings, "transfer_mode", "stub") or "stub").lower()
    endpoint = getattr(settings, "transfer_endpoint_url", "") or ""

    if mode != "live" or not endpoint:
        logger.info(
            "transfer STUB call_id=%s target=%s reason=%s (endpoint not configured)",
            call_id,
            target,
            reason,
        )
        return TransferResult(status="pending", disposition=DISP_PENDING)

    payload = _build_payload(call_id=call_id, target=target, settings=settings)
    headers: dict[str, str] = {"Content-Type": "application/json"}
    api_key = getattr(settings, "transfer_api_key", "") or ""
    token = getattr(settings, "transfer_auth_token", "") or ""
    if api_key:
        headers["X-API-Key"] = api_key
    elif token:
        headers["Authorization"] = f"Bearer {token}"
    timeout = float(getattr(settings, "transfer_timeout_s", 10.0) or 10.0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(endpoint, json=payload, headers=headers)
            resp.raise_for_status()
            detail = resp.json() if resp.content else {}
        logger.info("transfer LIVE initiated call_id=%s target=%s", call_id, target)
        return TransferResult(
            status="initiated", disposition=DISP_INITIATED, detail=detail
        )
    except Exception as exc:  # noqa: BLE001 — transfer must never crash the turn
        logger.warning("transfer LIVE failed call_id=%s err=%s", call_id, exc)
        return TransferResult(status="failed", disposition=DISP_FAILED)
