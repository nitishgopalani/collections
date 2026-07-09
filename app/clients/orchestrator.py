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


def _auth_headers() -> dict[str, str]:
    """Bearer token for orchestrator P3 auth (brain service key)."""
    key = (os.getenv("ORCHESTRATOR_SERVICE_KEY") or "").strip()
    if not key:
        return {}
    return {"Authorization": f"Bearer {key}"}


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = _base_url() + path
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT_S) as client:
            resp = client.post(url, json=payload, headers=_auth_headers())
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
    except Exception as exc:  # noqa: BLE001 — surface loudly, no silent retries
        logger.error("orchestrator POST %s failed: %s", path, exc)
        raise OrchestratorError(f"orchestrator {path} failed: {exc}") from exc
    logger.info("orchestrator POST %s ok payload=%s", path, payload)
    if not isinstance(data, dict):
        return {}
    return _normalize_response(data)


def _get(path: str) -> dict[str, Any]:
    url = _base_url() + path
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT_S) as client:
            resp = client.get(url, headers=_auth_headers())
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
    except Exception as exc:  # noqa: BLE001 — surface loudly, no silent retries
        logger.error("orchestrator GET %s failed: %s", path, exc)
        raise OrchestratorError(f"orchestrator {path} failed: {exc}") from exc
    if not isinstance(data, dict):
        return {}
    return _normalize_response(data)


def _normalize_response(data: dict[str, Any]) -> dict[str, Any]:
    """Ensure canonical ``id`` and deprecated op-id aliases are present."""
    out = dict(data)
    op_id = (
        out.get("id")
        or out.get("consult_id")
        or out.get("transfer_id")
        or out.get("conference_id")
    )
    if not op_id:
        return out
    out["id"] = str(op_id)
    if str(op_id).startswith("consult-"):
        out.setdefault("consult_id", op_id)
    elif str(op_id).startswith("transfer-"):
        out.setdefault("transfer_id", op_id)
    elif str(op_id).startswith("conf-"):
        out.setdefault("conference_id", op_id)
    return out


def status_matches(payload: dict[str, Any], *wants: str) -> bool:
    """True when an orchestrator status payload matches any wanted lifecycle value.

    Accepts both the P1 public vocabulary (``pending``/``active``/``ended``) and
    legacy internal names (``up``/``retrying``/``finished``).
    """
    status = str(payload.get("status") or "")
    detail = str(payload.get("detail") or "")
    for want in wants:
        if status == want:
            return True
        if want in ("originating", "joining") and status == "pending":
            return detail in ("", want)
        if want == "retrying" and status == "pending" and detail == "retrying":
            return True
        if want == "up" and status == "active":
            return True
        if want in ("bridged", "conferenced") and status == "active" and detail == want:
            return True
        if want in ("finished", "completed", "cancelled", "left") and status == "ended":
            return True
    return False


def originate(
    *, to: str, caller_id: str = "", context: str | None = None, destination: str = ""
) -> dict[str, Any]:
    """Dial a new channel. Returns ``{channel_id, status, id?, request_id}``."""
    target = (to or destination).strip()
    payload: dict[str, Any] = {"to": target, "caller_id": caller_id}
    if context:
        payload["context"] = context
    return _post("/v1/originate", payload)


def hangup(*, channel_id: str) -> dict[str, Any]:
    """Hang up a channel."""
    return _post("/v1/hangup", {"channel_id": channel_id})


def transfer(
    *,
    existing_channel_id: str = "",
    session_uuid: str = "",
    to: str = "",
    transfer_to: str = "",
    caller_id: str = "",
    ring_budget_s: float | None = None,
) -> dict[str, Any]:
    """Bridge a human onto an existing live call (cold transfer)."""
    payload: dict[str, Any] = {
        "to": (to or transfer_to).strip(),
        "caller_id": caller_id,
    }
    if session_uuid:
        payload["session_uuid"] = session_uuid
    elif existing_channel_id:
        payload["existing_channel_id"] = existing_channel_id
    if ring_budget_s is not None and ring_budget_s > 0:
        payload["ring_budget_s"] = ring_budget_s
    return _post("/v1/transfer", payload)


def conference(
    *, channel_ids: Iterable[str], bridge_id: str | None = None
) -> dict[str, Any]:
    """Build (or extend) a mixing bridge from ``channel_ids``."""
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
    *,
    session_uuid: str,
    to: str = "",
    transfer_to: str = "",
    caller_id: str = "",
    ring_budget_s: float | None = None,
) -> dict[str, Any]:
    """Start a warm human handoff on a Stasis-owned inbound call."""
    payload: dict[str, Any] = {
        "session_uuid": session_uuid,
        "to": (to or transfer_to).strip(),
        "caller_id": caller_id,
    }
    if ring_budget_s is not None and ring_budget_s > 0:
        payload["ring_budget_s"] = ring_budget_s
    return _post("/v1/transfer", payload)


def transfer_complete(*, transfer_id: str = "", id: str = "") -> dict[str, Any]:
    """Finish a warm transfer: drop the AI leg, the agent owns the call."""
    op_id = (id or transfer_id).strip()
    return _post("/v1/transfer/complete", {"transfer_id": op_id, "id": op_id})


def transfer_cancel(*, transfer_id: str = "", id: str = "") -> dict[str, Any]:
    """Abort a warm transfer (no answer / declined); the AI keeps the call."""
    op_id = (id or transfer_id).strip()
    return _post("/v1/transfer/cancel", {"transfer_id": op_id, "id": op_id})


def transfer_status(*, transfer_id: str = "", id: str = "") -> dict[str, Any]:
    """Fetch a transfer's async state (public: pending|ringing|active|failed|ended)."""
    op_id = (id or transfer_id).strip()
    return _get(f"/v1/transfer/{op_id}")


def consult_start(
    *,
    session_uuid: str,
    to: str = "",
    consult_destination: str = "",
    caller_id: str = "",
    ring_budget_s: float | None = None,
    max_attempts: int | None = None,
    retry_gap_s: float | None = None,
) -> dict[str, Any]:
    """Put the customer on hold and dial a consult leg."""
    payload: dict[str, Any] = {
        "session_uuid": session_uuid,
        "to": (to or consult_destination).strip(),
        "caller_id": caller_id,
    }
    if ring_budget_s is not None and ring_budget_s > 0:
        payload["ring_budget_s"] = ring_budget_s
    if max_attempts is not None and max_attempts > 0:
        payload["max_attempts"] = max_attempts
    if retry_gap_s is not None and retry_gap_s > 0:
        payload["retry_gap_s"] = retry_gap_s
    return _post("/v1/consult/start", payload)


def consult_finish(
    *, consult_id: str = "", id: str = "", outcome: str = ""
) -> dict[str, Any]:
    """End the consult leg and take the customer off hold."""
    op_id = (id or consult_id).strip()
    return _post("/v1/consult/finish", {"consult_id": op_id, "id": op_id, "outcome": outcome})


def consult_hold_pause(*, consult_id: str = "", id: str = "") -> dict[str, Any]:
    """Stop bridge MOH and unmute the customer AI leg for a hold announcement."""
    op_id = (id or consult_id).strip()
    return _post(f"/v1/consult/{op_id}/hold-pause", {})


def consult_hold_resume(*, consult_id: str = "", id: str = "") -> dict[str, Any]:
    """Re-mute the customer AI leg and restart MOH after a hold announcement."""
    op_id = (id or consult_id).strip()
    return _post(f"/v1/consult/{op_id}/hold-resume", {})


def consult_status(*, consult_id: str = "", id: str = "") -> dict[str, Any]:
    """Fetch a consult's async state."""
    op_id = (id or consult_id).strip()
    return _get(f"/v1/consult/{op_id}")


def consult_machine_answer(*, consult_id: str = "", id: str = "") -> dict[str, Any]:
    """Report voicemail/machine on an answered consult leg; orchestrator retries."""
    op_id = (id or consult_id).strip()
    return _post(f"/v1/consult/{op_id}/machine-answer", {})


def conference_join(
    *,
    session_uuid: str,
    to: str = "",
    invite_number: str = "",
    caller_id: str = "",
    ring_budget_s: float | None = None,
) -> dict[str, Any]:
    """Originate a third party into an existing inbound conference bridge (CF1)."""
    payload: dict[str, Any] = {
        "session_uuid": session_uuid,
        "to": (to or invite_number).strip(),
        "caller_id": caller_id,
    }
    if ring_budget_s is not None and ring_budget_s > 0:
        payload["ring_budget_s"] = ring_budget_s
    return _post("/v1/conference/join", payload)


def conference_join_status(*, conference_id: str = "", id: str = "") -> dict[str, Any]:
    """Fetch CF1 conference join async state."""
    op_id = (id or conference_id).strip()
    return _get(f"/v1/conference/{op_id}")
