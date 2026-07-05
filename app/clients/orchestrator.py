"""HTTP client for the Phase B ``ari-orchestrator`` call-control service.

The brain declares *intent* in flow logic; the *mechanism* — originate, hang up,
bridge a human onto a live call (transfer), and build/manage multi-party
conferences — lives in the ari-orchestrator. This module is a thin synchronous
wrapper around its ``/v1`` HTTP API.

Config: ``ORCHESTRATOR_BASE_URL`` (e.g. ``http://127.0.0.1:8095``).

There is intentionally **no retry / circuit-breaker** logic here — matching the
Phase A connector's "no reconnect, fail loudly and log" stance. Any transport or
non-2xx error is logged and re-raised as :class:`OrchestratorError`; callers
decide how to degrade.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 10.0


class OrchestratorError(RuntimeError):
    """Raised when an orchestrator call cannot be completed."""


def _base_url() -> str:
    url = (os.getenv("ORCHESTRATOR_BASE_URL") or "").strip()
    if not url:
        raise OrchestratorError("ORCHESTRATOR_BASE_URL is not set")
    return url.rstrip("/")


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = _base_url() + path
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT_S) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
    except Exception as exc:  # noqa: BLE001 — surface loudly, no silent retries
        logger.error("orchestrator POST %s failed: %s", path, exc)
        raise OrchestratorError(f"orchestrator {path} failed: {exc}") from exc
    logger.info("orchestrator POST %s ok payload=%s", path, payload)
    if not isinstance(data, dict):
        return {}
    return data


def _get(path: str) -> dict[str, Any]:
    url = _base_url() + path
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT_S) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
    except Exception as exc:  # noqa: BLE001 — surface loudly, no silent retries
        logger.error("orchestrator GET %s failed: %s", path, exc)
        raise OrchestratorError(f"orchestrator {path} failed: {exc}") from exc
    if not isinstance(data, dict):
        return {}
    return data


def originate(
    *, destination: str, caller_id: str = "", context: str | None = None
) -> dict[str, Any]:
    """Dial a new channel. Returns ``{channel_id, status}``."""
    payload: dict[str, Any] = {"destination": destination, "caller_id": caller_id}
    if context:
        payload["context"] = context
    return _post("/v1/originate", payload)


def hangup(*, channel_id: str) -> dict[str, Any]:
    """Hang up a channel."""
    return _post("/v1/hangup", {"channel_id": channel_id})


def transfer(
    *, existing_channel_id: str, transfer_to: str, caller_id: str = ""
) -> dict[str, Any]:
    """Bridge a human (``transfer_to``) onto an existing live call.

    Returns ``{bridge_id, channel_ids, status}``.
    """
    payload: dict[str, Any] = {
        "existing_channel_id": existing_channel_id,
        "transfer_to": transfer_to,
    }
    if caller_id:
        payload["caller_id"] = caller_id
    return _post("/v1/transfer", payload)


def conference(
    *, channel_ids: Iterable[str], bridge_id: str | None = None
) -> dict[str, Any]:
    """Build (or extend) a mixing bridge from ``channel_ids``.

    Returns ``{bridge_id, channel_ids, status}``.
    """
    payload: dict[str, Any] = {"channel_ids": list(channel_ids)}
    if bridge_id:
        payload["bridge_id"] = bridge_id
    return _post("/v1/conference", payload)


def participant(*, bridge_id: str, channel_id: str, action: str) -> dict[str, Any]:
    """Manage a channel's membership in a bridge (``add`` | ``remove`` | ``mute``)."""
    return _post(
        "/v1/participant",
        {"bridge_id": bridge_id, "channel_id": channel_id, "action": action},
    )


def warm_transfer(
    *, session_uuid: str, transfer_to: str, caller_id: str = ""
) -> dict[str, Any]:
    """Start a warm human handoff on a Stasis-owned inbound call.

    ``session_uuid`` is the brain's own session_id (the AudioSocket uuid the
    orchestrator minted; dash-less form accepted). The orchestrator dials the
    agent; when they ANSWER they join the customer's existing bridge —
    three-way (customer + AI + agent). Nothing is held and the AI leg stays up,
    so the bot can announce the handoff. Poll :func:`transfer_status` for
    ``up``, then call :func:`transfer_complete` to drop the AI leg (the humans
    stay bridged), or :func:`transfer_cancel` on no-answer/decline (the
    customer keeps talking to the AI).

    Returns ``{transfer_id, session_uuid, bridge_id, channel_ids,
    agent_channel_id, status}``.
    """
    payload: dict[str, Any] = {
        "session_uuid": session_uuid,
        "transfer_to": transfer_to,
    }
    if caller_id:
        payload["caller_id"] = caller_id
    return _post("/v1/transfer", payload)


def transfer_complete(*, transfer_id: str) -> dict[str, Any]:
    """Finish a warm transfer: drop the AI leg, the agent owns the call."""
    return _post("/v1/transfer/complete", {"transfer_id": transfer_id})


def transfer_cancel(*, transfer_id: str) -> dict[str, Any]:
    """Abort a warm transfer (no answer / declined); the AI keeps the call."""
    return _post("/v1/transfer/cancel", {"transfer_id": transfer_id})


def transfer_status(*, transfer_id: str) -> dict[str, Any]:
    """Fetch a transfer's async state.

    ``status`` is one of ``originating|ringing|up|failed|completed|cancelled|
    finished``; the response also carries ``customer_channel_id`` and
    ``agent_channel_id``.
    """
    return _get(f"/v1/transfer/{transfer_id}")


def consult_start(
    *, session_uuid: str, consult_destination: str, caller_id: str = ""
) -> dict[str, Any]:
    """Put the customer on hold (AI leg out + bridge MOH) and dial a consult leg.

    ``session_uuid`` is the brain's own session_id — the AudioSocket uuid the
    orchestrator minted for the Stasis-inbound call, which its registry resolves
    to the real customer channel/bridge (dash-less form accepted).

    Returns ``{consult_id, bridge_id, consult_channel_id, session_uuid,
    consult_uuid, status}``. ``consult_uuid`` is the session_id the connector
    will open for the consult leg's OWN AI leg — register the property persona
    binding under it (see app/engine/consult_binding.py). The consult leg
    answers asynchronously — poll :func:`consult_status` for ``up``/``failed``.
    """
    return _post(
        "/v1/consult/start",
        {
            "session_uuid": session_uuid,
            "consult_destination": consult_destination,
            "caller_id": caller_id,
        },
    )


def consult_finish(*, consult_id: str, outcome: str = "") -> dict[str, Any]:
    """End the consult leg and take the customer off hold."""
    return _post("/v1/consult/finish", {"consult_id": consult_id, "outcome": outcome})


def consult_status(*, consult_id: str) -> dict[str, Any]:
    """Fetch a consult's async state (``originating|ringing|up|failed|finished``)."""
    return _get(f"/v1/consult/{consult_id}")
