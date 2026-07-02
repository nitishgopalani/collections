"""Live call-transfer provider (Model A: brain calls the transfer endpoint).

The *decision* to transfer is made by flow logic (any ``action: transfer_call``
step, after the flow has spoken a "connecting you to an agent" line). The
*mechanism* — bridging a live human onto the same call — is this module.

Today the real endpoint (from telephony) is not available yet, so the default
mode is ``stub``: we log the intent and return ``pending`` (the flow still speaks
the handoff line and ends the bot leg, so a test call behaves sensibly). When the
endpoint is ready, set ``TRANSFER_MODE=live`` + ``TRANSFER_ENDPOINT_URL`` (and auth)
and this same code POSTs to it — no other changes needed.

INTEGRATION POINT: confirm the endpoint's request/response contract with the
telephony team and adjust ``_build_payload`` / result parsing below. In
particular confirm which call identifier the endpoint keys on (we pass
``call_id`` = the WS ``session_id``/uuid).
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


def _build_payload(*, call_id: str, target: str, reason: str) -> dict[str, Any]:
    # INTEGRATION POINT: match the telephony endpoint's expected body.
    payload: dict[str, Any] = {"call_id": call_id, "reason": reason}
    if target:
        payload["target"] = target
    return payload


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

    payload = _build_payload(call_id=call_id, target=target, reason=reason)
    headers: dict[str, str] = {}
    token = getattr(settings, "transfer_auth_token", "") or ""
    if token:
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
