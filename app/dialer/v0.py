"""Campaign originate gate — DNC / cadence / active-call / callback consume."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.config import get_settings
from app.engine.dialer_controls import get_controls, today_ist

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dialer/v0", tags=["dialer-v0"])


class OriginateIn(BaseModel):
    borrower_id: str = ""
    phone: str = ""
    tenant_id: str = ""
    dry_run: bool = False
    place_call: bool = False
    day: str | None = None


class CompleteIn(BaseModel):
    borrower_id: str = ""
    phone: str = ""


def _day(raw: str | None) -> date:
    if raw:
        return date.fromisoformat(raw)
    return today_ist()


def _max_attempts() -> int:
    return int(get_settings().dialer_max_attempts_per_day)


def _maybe_orch(phone: str, caller_id: str = "") -> dict[str, Any] | None:
    settings = get_settings()
    if not (settings.orchestrator_base_url or "").strip():
        return None
    if not phone:
        return None
    from app.clients.orchestrator import originate

    return originate(to=phone, caller_id=caller_id)


@router.post("/originate")
async def originate_campaign(body: OriginateIn) -> dict[str, Any]:
    settings = get_settings()
    if not settings.dialer_gate_enabled:
        raise HTTPException(status_code=503, detail="dialer gate disabled")
    controls = get_controls()
    day = _day(body.day)
    cap = _max_attempts()
    if body.dry_run:
        decision = controls.check_originate(
            borrower_id=body.borrower_id,
            phone=body.phone,
            day=day,
            max_attempts=cap,
        )
    else:
        decision = controls.commit_originate(
            borrower_id=body.borrower_id,
            phone=body.phone,
            tenant_id=body.tenant_id,
            day=day,
            max_attempts=cap,
        )
    payload = decision.as_dict()
    payload["dry_run"] = body.dry_run
    if not decision.allow:
        status = {
            "dnc_suppressed": 403,
            "cadence_blocked": 429,
            "active_call": 409,
            "missing_identity": 422,
        }.get(decision.reason, 400)
        raise HTTPException(status_code=status, detail=payload)
    if not body.dry_run and body.place_call:
        try:
            orch = _maybe_orch(decision.phone)
            if orch is not None:
                payload["orchestrator"] = orch
        except Exception as exc:  # noqa: BLE001 — gate already committed; log loud
            logger.error("orchestrator originate after gate failed: %s", exc)
            payload["orchestrator_error"] = str(exc)
    return payload


@router.post("/complete")
async def complete_call(body: CompleteIn) -> dict[str, Any]:
    get_controls().release(body.borrower_id, body.phone)
    return {"ok": True}


@router.get("/callbacks")
async def list_callbacks(date_stamp: str = Query(..., alias="date", min_length=8, max_length=8)) -> dict[str, Any]:
    day = date.fromisoformat(f"{date_stamp[:4]}-{date_stamp[4:6]}-{date_stamp[6:8]}")
    from app.engine.obligation_export import exports_root, read_jsonl

    path = exports_root() / f"callbacks_{date_stamp}.jsonl"
    return {"date": date_stamp, "rows": read_jsonl(path)}


@router.post("/callbacks/consume")
async def consume_callbacks(
    date_stamp: str = Query(..., alias="date", min_length=8, max_length=8),
    commit: bool = False,
) -> dict[str, Any]:
    day = date.fromisoformat(f"{date_stamp[:4]}-{date_stamp[4:6]}-{date_stamp[6:8]}")
    return get_controls().consume_callbacks(day, max_attempts=_max_attempts(), commit=commit)
