"""Live WhatsApp sender (payment-link / reminder template via the campaign creator).

The *decision* to send is made by flow logic (any ``action: send_whatsapp_message``
step, e.g. the mandatory sot_close). The *mechanism* — firing the templated WhatsApp
message — is this module.

Set ``WHATSAPP_MODE=stub`` (default) to just log intent (the sim in
``sot_tools_sim`` handles the fake link). Set ``WHATSAPP_MODE=live`` +
``WHATSAPP_ENDPOINT_URL`` + ``WHATSAPP_API_KEY`` (+ template/campaign names) and this
code POSTs to the campaign creator.

Contract (app.fonada.ai /functions/v1/whatsapp_campaign_creator): POST JSON
``{campaign_name, template_name, audience_rows:[{Phone, BODY_1}]}`` with header
``Authorization: Bearer <key>``. ``Phone`` is the borrower's number (digits with
country code) that the Go server received from the dialer; ``BODY_1`` is the
borrower's name (the template's first body variable).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings
from app.util.phone import digits_only

logger = logging.getLogger(__name__)


@dataclass
class WhatsappResult:
    """Outcome of a WhatsApp send attempt.

    status:
      - ``pending``   — endpoint not configured (stub); no live message went out.
      - ``sent``      — campaign creator accepted the request.
      - ``failed``    — endpoint errored/timed out.
    """

    status: str
    detail: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("pending", "sent")


def _build_payload(*, phone: str, name: str, settings: Any) -> dict[str, Any]:
    """Build the whatsapp_campaign_creator body.

    ``phone`` is normalized to digits-with-country-code (``+919810…`` -> ``919810…``),
    which is what the campaign API expects for the ``Phone`` audience field.
    """
    return {
        "campaign_name": getattr(settings, "whatsapp_campaign_name", "emi_campaign")
        or "emi_campaign",
        "template_name": getattr(settings, "whatsapp_template_name", "") or "",
        "audience_rows": [{"Phone": digits_only(phone), "BODY_1": name}],
    }


async def send_whatsapp(*, phone: str, name: str) -> WhatsappResult:
    """Send the templated WhatsApp message. Never raises — failures return ``failed``."""
    settings = get_settings()
    mode = (getattr(settings, "whatsapp_mode", "stub") or "stub").lower()
    endpoint = getattr(settings, "whatsapp_endpoint_url", "") or ""

    if mode != "live" or not endpoint:
        logger.info(
            "whatsapp STUB phone=%s name=%s (endpoint not configured)", phone, name
        )
        return WhatsappResult(status="pending")

    if not digits_only(phone):
        logger.warning("whatsapp LIVE skipped: no phone (name=%s)", name)
        return WhatsappResult(status="failed")

    payload = _build_payload(phone=phone, name=name, settings=settings)
    headers: dict[str, str] = {"Content-Type": "application/json"}
    api_key = getattr(settings, "whatsapp_api_key", "") or ""
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    timeout = float(getattr(settings, "whatsapp_timeout_s", 10.0) or 10.0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(endpoint, json=payload, headers=headers)
            resp.raise_for_status()
            detail = resp.json() if resp.content else {}
        logger.info("whatsapp LIVE sent phone=%s name=%s", digits_only(phone), name)
        return WhatsappResult(status="sent", detail=detail)
    except Exception as exc:  # noqa: BLE001 — send must never crash the turn
        logger.warning("whatsapp LIVE failed phone=%s err=%s", digits_only(phone), exc)
        return WhatsappResult(status="failed")
