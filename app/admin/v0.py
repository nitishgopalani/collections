"""Brand Console v0 — /admin/v0 router. Env-gated, CORS localhost:5173 only."""

from __future__ import annotations

import io
import logging
import struct
import uuid
import wave
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from app.admin.audit import audit_write, file_hash
from app.admin.replies import (
    find_flow_file,
    lookup_reply,
    validate_reply_text,
    write_flow_reply,
    write_fragment_reply,
)
from app.admin.yaml_io import (
    DEFAULT_PACE,
    DEFAULT_VOICES,
    VOICE_CATALOG,
    apply_profile_patch,
    dump_raw,
    fragments_path,
    list_tenant_ids,
    load_raw,
    profile_path,
    validate_profile_patch,
)
from app.clients.tools_sim import FakeToolClient
from app.config import get_settings, tenant_config
from app.engine.compliance_rules import evaluate_pressure_with_allowlist, matches_any
from app.engine.fragment_library import get_fragment, list_fragments
from app.engine.obligation_export import exports_root
from app.engine.tenant_profile import get_tenant_profile
from app.engine.turn import handle_turn
from app.flows.loader import reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/v0", tags=["admin-v0"])

_SESSIONS: dict[str, InMemoryMemoryStore] = {}
_SESSION_LOGS: dict[str, list[dict[str, Any]]] = {}
_EDITED_REPLY_IDS: set[str] = set()


class ProfilePut(BaseModel):
    yaml_hash: str | None = None
    patch: dict[str, Any] = Field(default_factory=dict)


class FragmentPut(BaseModel):
    yaml_hash: str | None = None
    text: str | None = None
    answers: list[str] | None = None
    safe_in: str | None = None
    category: str | None = None
    scenario: list[str] | None = None
    product: list[str] | None = None
    variants: dict[str, str] | None = None
    active: bool | None = None


class DryRunIn(BaseModel):
    texts: list[str] | None = None
    fragment_ids: list[str] | None = None


class TtsPreviewIn(BaseModel):
    text: str
    voice_id: str = "neha"
    pace: float = 1.1


class TestTurnIn(BaseModel):
    session_id: str | None = None
    transcript: str = ""
    scenario: str | None = None
    borrower_id: str | None = None


class ReplyPut(BaseModel):
    yaml_hash: str | None = None
    text: str
    attempt: int | None = None
    variants: list[dict[str, Any]] | None = None


class ReplayIn(BaseModel):
    session_id: str
    turn_index: int


def _require_enabled() -> None:
    if not get_settings().admin_api_enabled:
        raise HTTPException(status_code=404, detail="admin api disabled")


def _tenant_or_404(tenant_id: str):
    profile = get_tenant_profile(tenant_id)
    if profile is None and tenant_id not in list_tenant_ids():
        raise HTTPException(status_code=404, detail="unknown tenant")
    return profile


@router.get("/tenants")
async def list_tenants() -> dict[str, Any]:
    _require_enabled()
    rows = []
    for tid in list_tenant_ids():
        prof = get_tenant_profile(tid)
        rows.append(
            {
                "tenant_id": tid,
                "has_profile": prof is not None,
                "has_fragments": fragments_path(tid).exists(),
            }
        )
    return {"tenants": rows}


@router.get("/tenant/{tenant_id}/profile")
async def get_profile(tenant_id: str) -> dict[str, Any]:
    _require_enabled()
    _tenant_or_404(tenant_id)
    path = profile_path(tenant_id)
    raw = load_raw(path)
    prof = get_tenant_profile(tenant_id)
    cue_sizes = {name: len(cues) for name, cues in (prof.cue_packs if prof else {}).items()}
    voices = dict(DEFAULT_VOICES)
    voices.update(raw.get("scenario_voices") or (prof.scenario_voices if prof else {}))
    pace = dict(DEFAULT_PACE)
    pace.update(raw.get("scenario_pace") or (prof.scenario_pace if prof else {}))
    ptp = dict((prof.ptp_policy if prof else None) or raw.get("ptp_policy") or {})
    digest = file_hash(path)
    return {
        "tenant_id": tenant_id,
        "yaml_hash": digest,
        "editable": {
            "dpdp_third_party_lock": (prof.dpdp_third_party_lock if prof else "strict"),
            "dpdp_disclosure_tier_enforced": (
                prof.dpdp_disclosure_tier_enforced if prof else True
            ),
            "call_window_start": (prof.call_window_start if prof else "")
            or get_settings().call_window_start,
            "call_window_end": (prof.call_window_end if prof else "")
            or get_settings().call_window_end,
            "call_window_timezone": (prof.call_window_timezone if prof else "")
            or get_settings().call_window_timezone,
            "ptp_policy": {
                "max_ptp_days": ptp.get("max_ptp_days", 30),
                "min_partial_pct": ptp.get("min_partial_pct", 25),
                "counter_max_attempts": ptp.get("counter_max_attempts", 1),
            },
            "frustration_escalate_turns": (
                prof.frustration_escalate_turns if prof else 3
            ),
            "max_slot_retries": (
                prof.max_slot_retries if prof and prof.max_slot_retries is not None else 2
            ),
            "scenario_voices": voices,
            "scenario_pace": pace,
            "variant_tone": (prof.variant_tone if prof else "") or raw.get("variant_tone") or "",
        },
        "readonly": {
            "cue_pack_sizes": cue_sizes,
            "backchannel_count": len(prof.backchannel_tokens) if prof else 0,
            "voice_catalog": list(VOICE_CATALOG),
        },
    }


@router.put("/tenant/{tenant_id}/profile")
async def put_profile(tenant_id: str, body: ProfilePut) -> dict[str, Any]:
    _require_enabled()
    _tenant_or_404(tenant_id)
    path = profile_path(tenant_id)
    before = file_hash(path)
    if body.yaml_hash and body.yaml_hash != before:
        raise HTTPException(status_code=409, detail="yaml_hash mismatch")
    errors = validate_profile_patch(body.patch)
    if errors:
        return JSONResponse(status_code=422, content={"ok": False, "errors": errors})
    raw = load_raw(path)
    updated = apply_profile_patch(raw, body.patch)
    dump_raw(path, updated)
    after = file_hash(path)
    audit_write(
        f"PUT /admin/v0/tenant/{tenant_id}/profile",
        before=before,
        after=after,
    )
    return {"ok": True, "yaml_hash": after, "errors": []}


@router.get("/tenant/{tenant_id}/fragments")
async def get_fragments(tenant_id: str) -> dict[str, Any]:
    _require_enabled()
    _tenant_or_404(tenant_id)
    path = fragments_path(tenant_id)
    rows = []
    for frag in list_fragments(tenant_id):
        variants = frag.get("variants") or {}
        rows.append(
            {
                "id": frag.get("id"),
                "category": frag.get("category"),
                "text": frag.get("text"),
                "answers": frag.get("answers") or [],
                "safe_in": frag.get("safe_in"),
                "scenario": frag.get("scenario") or [],
                "product": frag.get("product") or [],
                "slots": frag.get("slots") or [],
                "role": frag.get("role"),
                "variant_count": len(variants) if isinstance(variants, dict) else 0,
                "variants": variants if isinstance(variants, dict) else {},
                "allowlist": bool(frag.get("allowlist")),
                "active": frag.get("active", True) is not False,
            }
        )
    return {
        "tenant_id": tenant_id,
        "yaml_hash": file_hash(path) if path.exists() else "",
        "fragments": rows,
    }


@router.put("/tenant/{tenant_id}/fragment/{fid}")
async def put_fragment(tenant_id: str, fid: str, body: FragmentPut) -> dict[str, Any]:
    _require_enabled()
    _tenant_or_404(tenant_id)
    path = fragments_path(tenant_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="no fragment library")
    before = file_hash(path)
    if body.yaml_hash and body.yaml_hash != before:
        raise HTTPException(status_code=409, detail="yaml_hash mismatch")
    check_texts: list[str] = []
    if body.text:
        check_texts.append(body.text)
    if body.variants:
        check_texts.extend(str(v) for v in body.variants.values() if v)
    blocked = any(_line_verdict(t, tenant_id)["verdict"] == "fail" for t in check_texts)
    want_active = True if body.active is None else body.active
    if blocked and want_active:
        raise HTTPException(
            status_code=422,
            detail="blocked lines cannot be saved as active",
        )
    raw = load_raw(path)
    frags = list(raw.get("fragments") or [])
    found = None
    for item in frags:
        if isinstance(item, dict) and item.get("id") == fid:
            found = item
            break
    if found is None:
        raise HTTPException(status_code=404, detail="unknown fragment")
    if body.text is not None:
        found["text"] = body.text
    if body.answers is not None:
        found["answers"] = body.answers
    if body.safe_in is not None:
        found["safe_in"] = body.safe_in
    if body.category is not None:
        found["category"] = body.category
    if body.scenario is not None:
        found["scenario"] = body.scenario
    if body.product is not None:
        found["product"] = body.product
    if body.variants is not None:
        found["variants"] = body.variants
    if body.active is not None:
        found["active"] = body.active
    raw["fragments"] = frags
    dump_raw(path, raw)
    after = file_hash(path)
    audit_write(
        f"PUT /admin/v0/tenant/{tenant_id}/fragment/{fid}",
        before=before,
        after=after,
    )
    return {"ok": True, "yaml_hash": after, "fragment": get_fragment(tenant_id, fid)}


def _line_verdict(text: str, tenant_id: str) -> dict[str, Any]:
    cfg = tenant_config(tenant_id)
    prohibited = matches_any(text, cfg.prohibited_outbound_phrases)
    if prohibited:
        return {"text": text, "verdict": "fail", "reason": f"prohibited:{prohibited}"}
    blocking, warnings = evaluate_pressure_with_allowlist(
        text,
        cfg.collection_pressure_phrases,
        list(getattr(get_tenant_profile(tenant_id), "gate_allowlisted_phrases", None) or []),
    )
    if blocking:
        return {"text": text, "verdict": "fail", "reason": f"pressure:{blocking}"}
    if warnings:
        return {
            "text": text,
            "verdict": "allowlisted",
            "reason": warnings[0].get("phrase") if warnings else "allowlisted",
        }
    return {"text": text, "verdict": "pass", "reason": ""}


@router.post("/tenant/{tenant_id}/compliance-dry-run")
async def compliance_dry_run(tenant_id: str, body: DryRunIn) -> dict[str, Any]:
    _require_enabled()
    _tenant_or_404(tenant_id)
    lines: list[str] = []
    if body.texts:
        lines.extend(body.texts)
    elif body.fragment_ids:
        for fid in body.fragment_ids:
            frag = get_fragment(tenant_id, fid)
            if frag and frag.get("text"):
                lines.append(str(frag["text"]))
    else:
        lines.extend(str(f.get("text") or "") for f in list_fragments(tenant_id))
    results = [_line_verdict(t, tenant_id) for t in lines if t.strip()]
    return {"tenant_id": tenant_id, "results": results}


def _silent_wav(duration_s: float = 0.4) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        n = int(16000 * duration_s)
        wf.writeframes(struct.pack("<" + "h" * n, *([0] * n)))
    return buf.getvalue()


@router.post("/tts-preview")
async def tts_preview(body: TtsPreviewIn) -> Response:
    _require_enabled()
    settings = get_settings()
    key = (settings.sarvam_api_key or "").strip()
    if not key or not (body.text or "").strip():
        return Response(content=_silent_wav(), media_type="audio/wav")
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                settings.sarvam_tts_url,
                headers={"api-subscription-key": key, "Content-Type": "application/json"},
                json={
                    "text": body.text,
                    "target_language_code": "hi-IN",
                    "speaker": body.voice_id,
                    "pace": body.pace,
                    "model": "bulbul:v2",
                    "speech_sample_rate": 16000,
                },
            )
        if resp.status_code >= 400:
            logger.warning("sarvam tts-preview status=%s", resp.status_code)
            return Response(content=_silent_wav(), media_type="audio/wav")
        data = resp.json()
        audio_b64 = (data.get("audios") or [None])[0]
        if not audio_b64:
            return Response(content=_silent_wav(), media_type="audio/wav")
        import base64

        return Response(content=base64.b64decode(audio_b64), media_type="audio/wav")
    except Exception:
        logger.exception("sarvam tts-preview failed")
        return Response(content=_silent_wav(), media_type="audio/wav")


def _test_turn_meta(tenant_id: str, scenario: str | None) -> dict[str, Any]:
    meta: dict[str, Any] = {"call_date": "2026-08-15"}
    if scenario:
        prof = get_tenant_profile(tenant_id)
        slot = (prof.test_scenario_override_slot if prof else "") or "plo_scenario_override"
        meta["force_flow"] = f"{(prof.flow_prefix if prof else 'plo_')}opener"
        meta["scenario_override"] = scenario.strip().lower()
        meta["scenario_override_slot"] = slot
    return meta


def _pack_test_turn(
    *,
    session_id: str,
    result: Any,
    guards: dict[str, Any],
    turn_index: int,
) -> dict[str, Any]:
    reply_id = result.reply_id
    return {
        "session_id": session_id,
        "reply_text": result.reply_text,
        "reply_id": reply_id,
        "end_call": result.end_call,
        "disposition": result.disposition or guards.get("disposition"),
        "turn_index": turn_index,
        "edited_by_console": bool(reply_id and reply_id in _EDITED_REPLY_IDS),
        "guards": {
            "evidence": guards.get("evidence"),
            "evidence_reason": guards.get("evidence_reason"),
            "gate_verdict": guards.get("gate_verdict"),
            "oof_class": guards.get("oof_class"),
            "oof_subclass": guards.get("oof_subclass"),
            "fragment_ids": guards.get("fragment_ids") or [],
            "disposition": guards.get("disposition") or result.disposition,
            "llm_call_reason": guards.get("llm_call_reason"),
        },
    }


async def _execute_test_turn(
    request: Request,
    *,
    tenant_id: str,
    session_id: str,
    store: InMemoryMemoryStore,
    transcript: str,
    scenario: str | None,
    borrower_id: str,
) -> tuple[Any, dict[str, Any]]:
    req = TurnRequest(
        call_id=session_id,
        borrower_id=borrower_id,
        tenant_id=tenant_id,
        channel="voice",
        locale="hi-IN",
        transcript=transcript,
        turn_meta=_test_turn_meta(tenant_id, scenario),
    )
    app = request.app
    result = await handle_turn(
        req,
        memory=store,
        kb=app.state.kb,
        llm=app.state.llm,
        tools=FakeToolClient(),
        flows=getattr(app.state, "flows", None),
    )
    state = await store.load_state(session_id)
    guards = dict((state.slots.get("_last_guards") if state else None) or {})
    return result, guards


@router.post("/tenant/{tenant_id}/test-turn")
async def test_turn(request: Request, tenant_id: str, body: TestTurnIn) -> dict[str, Any]:
    _require_enabled()
    _tenant_or_404(tenant_id)
    session_id = (body.session_id or "").strip() or uuid.uuid4().hex[:12]
    store = _SESSIONS.setdefault(session_id, InMemoryMemoryStore())
    borrower_id = body.borrower_id or (
        (get_tenant_profile(tenant_id).test_borrower_id if get_tenant_profile(tenant_id) else "")
        or "plo_test_borrower"
    )
    result, guards = await _execute_test_turn(
        request,
        tenant_id=tenant_id,
        session_id=session_id,
        store=store,
        transcript=body.transcript,
        scenario=body.scenario,
        borrower_id=borrower_id,
    )
    log = _SESSION_LOGS.setdefault(session_id, [])
    packed = _pack_test_turn(
        session_id=session_id,
        result=result,
        guards=guards,
        turn_index=len(log),
    )
    catalog = lookup_reply(tenant_id, result.reply_id or "")
    packed["source_kind"] = (catalog or {}).get("source_kind")
    packed["editable"] = bool((catalog or {}).get("editable"))
    log.append(
        {
            "transcript": body.transcript,
            "scenario": body.scenario,
            "borrower_id": borrower_id,
            "reply_id": result.reply_id,
            "reply_text": result.reply_text,
            "guards": packed["guards"],
        }
    )
    return packed


@router.post("/tenant/{tenant_id}/test-turn/replay")
async def replay_test_turn(request: Request, tenant_id: str, body: ReplayIn) -> dict[str, Any]:
    _require_enabled()
    _tenant_or_404(tenant_id)
    session_id = (body.session_id or "").strip()
    log = _SESSION_LOGS.get(session_id) or []
    if not session_id or body.turn_index < 0 or body.turn_index >= len(log):
        raise HTTPException(status_code=404, detail="unknown session or turn_index")
    seed = log[0]
    borrower_id = str(seed.get("borrower_id") or "plo_test_borrower")
    scenario = seed.get("scenario")
    store = InMemoryMemoryStore()
    last_result = None
    last_guards: dict[str, Any] = {}
    new_log: list[dict[str, Any]] = []
    for i, turn in enumerate(log[: body.turn_index + 1]):
        last_result, last_guards = await _execute_test_turn(
            request,
            tenant_id=tenant_id,
            session_id=session_id,
            store=store,
            transcript=str(turn.get("transcript") or ""),
            scenario=turn.get("scenario") or scenario,
            borrower_id=str(turn.get("borrower_id") or borrower_id),
        )
        new_log.append(
            {
                "transcript": turn.get("transcript") or "",
                "scenario": turn.get("scenario") or scenario,
                "borrower_id": turn.get("borrower_id") or borrower_id,
                "reply_id": last_result.reply_id,
                "reply_text": last_result.reply_text,
                "guards": dict(last_guards),
            }
        )
    _SESSIONS[session_id] = store
    _SESSION_LOGS[session_id] = new_log
    assert last_result is not None
    packed = _pack_test_turn(
        session_id=session_id,
        result=last_result,
        guards=last_guards,
        turn_index=body.turn_index,
    )
    catalog = lookup_reply(tenant_id, last_result.reply_id or "")
    packed["source_kind"] = (catalog or {}).get("source_kind")
    packed["editable"] = bool((catalog or {}).get("editable"))
    packed["truncated_after"] = body.turn_index
    return packed


@router.get("/tenant/{tenant_id}/reply/{reply_id}")
async def get_reply(tenant_id: str, reply_id: str) -> dict[str, Any]:
    _require_enabled()
    _tenant_or_404(tenant_id)
    row = lookup_reply(tenant_id, reply_id)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown reply_id")
    return row


@router.put("/tenant/{tenant_id}/reply/{reply_id}")
async def put_reply(tenant_id: str, reply_id: str, body: ReplyPut) -> dict[str, Any]:
    _require_enabled()
    _tenant_or_404(tenant_id)
    row = lookup_reply(tenant_id, reply_id)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown reply_id")
    if not row.get("editable"):
        raise HTTPException(status_code=422, detail=row.get("lock_reason") or "not editable")
    extra = []
    if body.variants:
        extra.extend(str(v.get("text") or "") for v in body.variants if v.get("text"))
    errors = validate_reply_text(body.text, extra_texts=extra)
    if errors:
        return JSONResponse(status_code=422, content={"ok": False, "errors": errors})
    check_texts = [body.text, *extra]
    blocked = any(_line_verdict(t, tenant_id)["verdict"] == "fail" for t in check_texts if t)
    if blocked:
        raise HTTPException(
            status_code=422,
            detail="blocked lines cannot be saved as active",
        )
    kind = row["source_kind"]
    if kind == "fragment":
        path = fragments_path(tenant_id)
        before = file_hash(path)
        if body.yaml_hash and body.yaml_hash != before:
            raise HTTPException(status_code=409, detail="yaml_hash mismatch")
        write_fragment_reply(tenant_id, reply_id, body.text)
        after = file_hash(path)
    else:
        path = find_flow_file(reply_id)
        if path is None:
            raise HTTPException(status_code=404, detail="reply file missing")
        before = file_hash(path)
        if body.yaml_hash and body.yaml_hash != before:
            raise HTTPException(status_code=409, detail="yaml_hash mismatch")
        write_flow_reply(reply_id, text=body.text, attempt=body.attempt)
        reload_flow_set()
        after = file_hash(path)
    audit_write(
        f"PUT /admin/v0/tenant/{tenant_id}/reply/{reply_id}",
        before=before,
        after=after,
    )
    _EDITED_REPLY_IDS.add(reply_id)
    return {"ok": True, "yaml_hash": after, "errors": [], "reply": lookup_reply(tenant_id, reply_id)}


@router.get("/exports")
async def get_exports(
    date: str = Query(..., min_length=8, max_length=8),
    kind: str = Query(..., pattern="^(dispositions|callbacks|worklist)$"),
) -> dict[str, Any]:
    _require_enabled()
    path = exports_root() / f"{kind}_{date}.jsonl"
    rows: list[dict[str, Any]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                import json

                rows.append(json.loads(line))
            except Exception:
                continue
    return {"date": date, "kind": kind, "rows": rows}
