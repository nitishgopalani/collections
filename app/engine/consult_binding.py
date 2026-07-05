"""Property-leg persona binding for the Stasis-inbound consult flow.

When the customer persona triggers a consult, the ari-orchestrator dials the
property owner and originates a NEW AudioSocket AI leg for that call under a
fresh uuid (returned by ``/v1/consult/start`` as ``consult_uuid``). The
connector then opens a brand-new brain session whose ``session_id`` IS that
uuid (dash-less — the connector strips dashes). Nothing else about that
session says "this is the property leg", so the customer session must
pre-register the booking context here, keyed by the consult uuid, and the WS
handler checks this registry on every ``session_start``:

* hit  -> the session runs as ``persona_property`` for the registered tenant,
  with the booking context (booking_id / hotel / guest / ...) merged into
  ``borrower_context`` so the persona's opening system line carries it.
* miss -> normal tenant/persona resolution, untouched.

Bindings are unregistered when the property session ends, and expire after
``CONSULT_BINDING_TTL_S`` (default 120s) in case the consult leg never
connects (telco reject, owner does not answer) — expiry is enforced lazily on
every register/lookup, no background task.

In-memory and per-process, exactly like prompt_agent's session state: one
brain process owns a call's both legs today. This moves to Redis if the brain
ever scales horizontally.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TTL_S = 120.0

# consult uuid (normalized) -> {"context": {...}, "registered_at": monotonic}.
_BINDINGS: dict[str, dict[str, Any]] = {}


def _ttl_s() -> float:
    try:
        return float(os.getenv("CONSULT_BINDING_TTL_S", "") or DEFAULT_TTL_S)
    except ValueError:
        return DEFAULT_TTL_S


def normalize_uuid(value: str) -> str:
    """Canonicalize a uuid for registry keys.

    The orchestrator mints RFC-4122 uuids (with dashes); the connector strips
    the dashes before using the uuid as the brain session id. Both spellings
    must resolve to the same binding.
    """
    return (value or "").strip().lower().replace("-", "")


def _prune(now: float) -> None:
    ttl = _ttl_s()
    expired = [key for key, b in _BINDINGS.items() if now - b["registered_at"] > ttl]
    for key in expired:
        del _BINDINGS[key]
        logger.info("consult binding expired (ttl=%ss) consult_uuid=%s", ttl, key)


def register(consult_uuid: str, context: dict[str, Any]) -> None:
    """Register the booking context for an upcoming property-leg session.

    ``context`` must include ``tenant_id`` and ``persona``; booking fields
    (booking_id, hotel, guest, ...) are merged into the property session's
    borrower_context by the WS handler.
    """
    key = normalize_uuid(consult_uuid)
    if not key:
        return
    now = time.monotonic()
    _prune(now)
    _BINDINGS[key] = {"context": dict(context), "registered_at": now}
    logger.info(
        "consult binding registered consult_uuid=%s tenant_id=%s booking_id=%s",
        key,
        context.get("tenant_id", ""),
        context.get("booking_id", ""),
    )


def lookup(session_id: str) -> dict[str, Any] | None:
    """Return the registered context for a starting session, or None.

    Non-destructive: the binding stays registered until :func:`unregister`
    (session end) or TTL expiry, so a reconnecting property leg still binds.
    """
    _prune(time.monotonic())
    binding = _BINDINGS.get(normalize_uuid(session_id))
    if binding is None:
        return None
    return dict(binding["context"])


def unregister(session_id: str) -> None:
    """Drop a binding (property session ended or consult abandoned)."""
    if _BINDINGS.pop(normalize_uuid(session_id), None) is not None:
        logger.info("consult binding unregistered consult_uuid=%s", normalize_uuid(session_id))


def reset() -> None:
    """Clear all bindings (test isolation)."""
    _BINDINGS.clear()
