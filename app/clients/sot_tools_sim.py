"""Simulated Salary On Time tools — log-and-pretend, no real side effects.

Phase-1 stand-ins for the script's three external tools. None of these touch a
real WhatsApp/payment/transfer/telephony provider; they only log the intended
payload and return a deterministic fake result so flows are testable end-to-end.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)


def send_whatsapp_message(
    borrower_id: str,
    phone: str,
    message: str,
) -> dict[str, Any]:
    """Pretend to send a WhatsApp payment link. Logs the payload, returns a fake link."""
    link = f"https://pay.sot.test/{uuid.uuid4()}"
    logger.info(
        "SIM send_whatsapp_message borrower_id=%s phone=%s message=%r link=%s",
        borrower_id,
        phone,
        message,
        link,
    )
    return {"sent": True, "link": link, "simulated": True}


def transfer_call(call_id: str, reason: str) -> dict[str, Any]:
    """Pretend to transfer the call to a senior agent. Does NOT actually transfer."""
    logger.info("SIM transfer_call call_id=%s reason=%s (no real transfer)", call_id, reason)
    return {"transferred": False, "simulated": True, "reason": reason}


def hangup_call(call_id: str) -> dict[str, Any]:
    """Pretend to hang up. The real hangup is driven by end_call → go-server."""
    logger.info("SIM hangup_call call_id=%s", call_id)
    return {"hung_up": True, "simulated": True}
