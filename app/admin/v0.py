"""Brand Console v0 — /admin/v0 router. Env-gated, CORS localhost:5173 only."""

from __future__ import annotations

import io
import json
import logging
import re
import struct
import uuid
import wave
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from app.admin.audit import audit_write, file_hash
from app.admin.flow_graph import (
    build_flow_graph,
    default_flow_id,
    live_position,
    tenant_catalog,
)
from app.admin.flow_health import (
    apply_graph_health,
    scan_tenant_health,
)
from app.admin.flow_gate import run_fixture_suite, run_matrix_suite
from app.admin.flow_layout import read_layout, write_layout
from app.admin.flow_versions import (
    list_versions,
    restore_files,
    revert as revert_version,
    snapshot,
)
from app.admin.flow_write import apply_graph_to_yaml, find_flow_yaml, rel_flow_path
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
from app.flows.loader import get_flow_set, reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.version import build_info

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


class FixtureSaveIn(BaseModel):
    session_id: str
    name: str | None = None
    tenant_id: str | None = None
    scenario: str | None = None
    borrower_id: str | None = None


_FIXTURE_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "console"


def _require_enabled() -> None:
    if not get_settings().admin_api_enabled:
        raise HTTPException(status_code=404, detail="admin api disabled")


def _tenant_or_404(tenant_id: str):
    profile = get_tenant_profile(tenant_id)
    if profile is None and tenant_id not in list_tenant_ids():
        raise HTTPException(status_code=404, detail="unknown tenant")
    return profile


@router.get("/version")
async def admin_version() -> dict[str, Any]:
    _require_enabled()
    return build_info()


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
    state: Any = None,
) -> dict[str, Any]:
    reply_id = result.reply_id
    packed = {
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
    packed.update(live_position(state, get_flow_set()))
    return packed


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
        flows=get_flow_set(),
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
        state=await store.load_state(session_id),
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
        state=await store.load_state(session_id),
    )
    catalog = lookup_reply(tenant_id, last_result.reply_id or "")
    packed["source_kind"] = (catalog or {}).get("source_kind")
    packed["editable"] = bool((catalog or {}).get("editable"))
    packed["truncated_after"] = body.turn_index
    return packed


@router.post("/tenant/{tenant_id}/test-turn/fixture")
async def save_regression_fixture(tenant_id: str, body: FixtureSaveIn) -> dict[str, Any]:
    """Write the in-memory session log into tests/fixtures/console/ (CP-TEST)."""
    _require_enabled()
    _tenant_or_404(tenant_id)
    session_id = (body.session_id or "").strip()
    log = _SESSION_LOGS.get(session_id) or []
    if not session_id or not log:
        raise HTTPException(status_code=404, detail="unknown session")
    raw_name = (body.name or session_id).strip() or session_id
    name = _FIXTURE_NAME_RE.sub("-", raw_name).strip(".-")[:80] or session_id
    seed = log[0]
    fixture = {
        "id": name,
        "tenant_id": tenant_id,
        "scenario": body.scenario or seed.get("scenario") or "postdue1",
        "borrower_id": body.borrower_id or seed.get("borrower_id") or "plo_test_borrower",
        "session_id": session_id,
        "turns": [
            {
                "transcript": turn.get("transcript") or "",
                "expect": {
                    "reply_id": turn.get("reply_id"),
                    "reply_empty": not str(turn.get("reply_text") or "").strip(),
                    "evidence_reason": (turn.get("guards") or {}).get("evidence_reason"),
                    "gate_verdict": (turn.get("guards") or {}).get("gate_verdict"),
                    "oof_class": (turn.get("guards") or {}).get("oof_class"),
                    "disposition": (turn.get("guards") or {}).get("disposition")
                    or turn.get("disposition"),
                },
            }
            for turn in log
        ],
    }
    _FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    path = _FIXTURES_DIR / f"{name}.json"
    path.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit_write(
        "fixture_save",
        before="",
        after=file_hash(path),
        extra={"session_id": session_id, "name": name},
    )
    rel = path.name
    try:
        rel = path.relative_to(Path(__file__).resolve().parents[2]).as_posix()
    except ValueError:
        rel = path.as_posix()
    return {"ok": True, "path": rel, "name": name, "turns": len(log)}


@router.get("/tenant/{tenant_id}/flow/health")
async def tenant_flow_health(tenant_id: str) -> dict[str, Any]:
    _require_enabled()
    _tenant_or_404(tenant_id)
    prof = get_tenant_profile(tenant_id)
    if prof is None:
        raise HTTPException(status_code=404, detail="unknown tenant")
    flow_set = get_flow_set()
    catalog = tenant_catalog(prof, flow_set)
    catalog_ids = {row["id"] for row in catalog}

    def _verdict(text: str) -> dict[str, Any]:
        return _line_verdict(text, tenant_id)

    graphs = []
    for row in catalog:
        g = build_flow_graph(row["id"], flow_set)
        if not g:
            continue
        apply_graph_health(
            g,
            flow_set,
            tenant_id=tenant_id,
            catalog_ids=catalog_ids,
            verdict_fn=_verdict,
        )
        graphs.append(g)
    summary = scan_tenant_health(graphs)
    summary["tenant_id"] = tenant_id
    summary["flow_count"] = len(graphs)
    return summary


class FlowValidateIn(BaseModel):
    flow_id: str
    nodes: list[dict[str, Any]] | None = None
    edges: list[dict[str, Any]] | None = None


@router.post("/tenant/{tenant_id}/flow/validate")
async def validate_flow(tenant_id: str, body: FlowValidateIn) -> dict[str, Any]:
    """Dry-run health report. Same function as the overlay. Writes nothing."""
    _require_enabled()
    _tenant_or_404(tenant_id)
    prof = get_tenant_profile(tenant_id)
    if prof is None:
        raise HTTPException(status_code=404, detail="unknown tenant")
    flow_set = get_flow_set()
    catalog_ids = {row["id"] for row in tenant_catalog(prof, flow_set)}
    if body.flow_id not in catalog_ids:
        raise HTTPException(status_code=404, detail="unknown flow_id")
    if body.nodes is not None and body.edges is not None:
        graph: dict[str, Any] = {
            "flow_id": body.flow_id,
            "description": "",
            "scenarios": [],
            "nodes": body.nodes,
            "edges": body.edges,
        }
    else:
        graph = build_flow_graph(body.flow_id, flow_set)
        if not graph:
            raise HTTPException(status_code=404, detail="unknown flow_id")
    apply_graph_health(
        graph,
        flow_set,
        tenant_id=tenant_id,
        catalog_ids=catalog_ids,
        verdict_fn=lambda text: _line_verdict(text, tenant_id),
    )
    health = graph.get("health") or {}
    return {
        "ok": int(health.get("errors") or 0) == 0,
        "written": False,
        "flow_id": body.flow_id,
        "health": health,
    }


def _sync_app_flows(request: Request) -> None:
    request.app.state.flows = get_flow_set()


def _gate_fail(
    *,
    failed_stage: str,
    stages: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "ok": False,
            "written": False,
            "failed_stage": failed_stage,
            "stages": stages,
            "errors": errors,
        },
    )


class FlowPublishIn(BaseModel):
    flow_id: str
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/tenant/{tenant_id}/flow/publish", response_model=None)
async def publish_flow(request: Request, tenant_id: str, body: FlowPublishIn) -> Any:
    """Publish gate: health → compliance → fixtures → matrix. Abort writes nothing."""
    _require_enabled()
    _tenant_or_404(tenant_id)
    prof = get_tenant_profile(tenant_id)
    if prof is None:
        raise HTTPException(status_code=404, detail="unknown tenant")
    flow_set = get_flow_set()
    catalog_ids = {row["id"] for row in tenant_catalog(prof, flow_set)}
    if body.flow_id not in catalog_ids:
        raise HTTPException(status_code=404, detail="unknown flow_id")

    stages: list[dict[str, Any]] = []
    graph: dict[str, Any] = {
        "flow_id": body.flow_id,
        "description": "",
        "scenarios": [],
        "nodes": body.nodes,
        "edges": body.edges,
    }
    apply_graph_health(
        graph,
        flow_set,
        tenant_id=tenant_id,
        catalog_ids=catalog_ids,
        verdict_fn=lambda text: _line_verdict(text, tenant_id),
    )
    health = graph.get("health") or {}
    health_errs = [i for i in (health.get("issues") or []) if i.get("level") == "error"]
    stages.append(
        {
            "id": "health",
            "ok": not health_errs,
            "errors": int(health.get("errors") or 0),
            "warnings": int(health.get("warnings") or 0),
            "issues": health_errs,
        }
    )
    if health_errs:
        return _gate_fail(failed_stage="health", stages=stages, errors=health_errs)

    compliance_fails: list[dict[str, Any]] = []
    for node in body.nodes:
        if str(node.get("kind") or "") != "utter":
            continue
        reply_id = str(node.get("reply_id") or "").strip()
        if not reply_id:
            continue
        new_text = str(node.get("full_text") or node.get("text") or "").strip()
        variants = flow_set.responses.get(reply_id) or []
        old_text = (variants[0].text or "").strip() if variants else ""
        if not new_text or new_text == old_text:
            continue
        verdict = _line_verdict(new_text, tenant_id)
        if verdict.get("verdict") == "fail":
            compliance_fails.append(
                {
                    "node_id": node.get("id"),
                    "reply_id": reply_id,
                    "cell": reply_id,
                    "reason": verdict.get("reason"),
                    "code": "compliance_fail",
                    "level": "error",
                    "detail": str(verdict.get("reason") or ""),
                }
            )
    stages.append({"id": "compliance", "ok": not compliance_fails, "failed": compliance_fails})
    if compliance_fails:
        return _gate_fail(
            failed_stage="compliance", stages=stages, errors=compliance_fails
        )

    yaml_path = find_flow_yaml(body.flow_id)
    if yaml_path is None:
        raise HTTPException(status_code=404, detail="unknown flow_id")
    rel = rel_flow_path(yaml_path)
    before_files = {rel: yaml_path.read_text(encoding="utf-8")}
    before_hash = file_hash(yaml_path)

    apply_graph_to_yaml(body.flow_id, nodes=body.nodes, edges=body.edges)
    try:
        fixtures = await run_fixture_suite()
        stages.append(
            {
                "id": "fixtures",
                "ok": bool(fixtures.get("ok")),
                "total": fixtures.get("total"),
                "passed": fixtures.get("passed"),
                "failed": fixtures.get("failed") or [],
            }
        )
        if not fixtures.get("ok"):
            restore_files(before_files)
            _sync_app_flows(request)
            errors = [
                {
                    "cell": row.get("cell") or row.get("id"),
                    "code": "fixture_fail",
                    "level": "error",
                    "detail": "; ".join(row.get("diffs") or []),
                }
                for row in (fixtures.get("failed") or [])
            ]
            return _gate_fail(failed_stage="fixtures", stages=stages, errors=errors)

        matrix = await run_matrix_suite()
        stages.append(
            {
                "id": "matrix",
                "ok": bool(matrix.get("ok")),
                "total": matrix.get("total"),
                "passed": matrix.get("passed"),
                "failed": matrix.get("failed") or [],
            }
        )
        if not matrix.get("ok"):
            restore_files(before_files)
            _sync_app_flows(request)
            errors = [
                {
                    "cell": row.get("cell") or row.get("id"),
                    "code": "matrix_fail",
                    "level": "error",
                    "detail": "; ".join(row.get("diffs") or []),
                    "scenario": row.get("scenario"),
                    "line": row.get("line"),
                }
                for row in (matrix.get("failed") or [])
            ]
            return _gate_fail(failed_stage="matrix", stages=stages, errors=errors)
    except Exception:
        restore_files(before_files)
        _sync_app_flows(request)
        raise

    if not list_versions(tenant_id):
        snapshot(
            tenant_id,
            flow_id=body.flow_id,
            files=before_files,
            note="pre-publish",
            version=1,
        )
    after_files = {rel: yaml_path.read_text(encoding="utf-8")}
    snap = snapshot(
        tenant_id,
        flow_id=body.flow_id,
        files=after_files,
        note="publish",
    )
    audit_write(
        f"POST /admin/v0/tenant/{tenant_id}/flow/publish",
        before=before_hash,
        after=file_hash(yaml_path),
        extra={"flow_id": body.flow_id, "version": snap["version"]},
    )
    _sync_app_flows(request)
    return {
        "ok": True,
        "written": True,
        "flow_id": body.flow_id,
        "version": snap["version"],
        "stages": stages,
    }


@router.get("/tenant/{tenant_id}/flow/versions")
async def get_flow_versions(tenant_id: str) -> dict[str, Any]:
    _require_enabled()
    _tenant_or_404(tenant_id)
    return {"tenant_id": tenant_id, "versions": list_versions(tenant_id)}


@router.post("/tenant/{tenant_id}/flow/revert/{version}")
async def revert_flow(request: Request, tenant_id: str, version: int) -> dict[str, Any]:
    _require_enabled()
    _tenant_or_404(tenant_id)
    try:
        result = revert_version(tenant_id, version)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="unknown version") from None
    _sync_app_flows(request)
    audit_write(
        f"POST /admin/v0/tenant/{tenant_id}/flow/revert/{version}",
        before="",
        after="",
        extra={"version": version, "restored": result.get("restored")},
    )
    return result


@router.get("/tenant/{tenant_id}/flows")
async def list_tenant_flows(
    tenant_id: str,
    scenario: str | None = Query(default=None),
) -> dict[str, Any]:
    _require_enabled()
    _tenant_or_404(tenant_id)
    prof = get_tenant_profile(tenant_id)
    if prof is None:
        raise HTTPException(status_code=404, detail="unknown tenant")
    flow_set = get_flow_set()
    catalog = tenant_catalog(prof, flow_set, scenario=scenario)
    return {
        "tenant_id": tenant_id,
        "scenario": scenario,
        "default_flow_id": default_flow_id(prof.flow_prefix, scenario),
        "catalog": catalog,
    }


@router.get("/tenant/{tenant_id}/flow/{flow_id}/graph")
async def get_flow_graph(
    tenant_id: str,
    flow_id: str,
    scenario: str | None = Query(default=None),
) -> dict[str, Any]:
    _require_enabled()
    _tenant_or_404(tenant_id)
    prof = get_tenant_profile(tenant_id)
    if prof is None:
        raise HTTPException(status_code=404, detail="unknown tenant")
    flow_set = get_flow_set()
    catalog = tenant_catalog(prof, flow_set, scenario=scenario)
    allowed = {row["id"] for row in tenant_catalog(prof, flow_set)}
    if flow_id not in allowed:
        raise HTTPException(status_code=404, detail="unknown flow_id")
    graph = build_flow_graph(flow_id, flow_set)
    if not graph:
        raise HTTPException(status_code=404, detail="unknown flow_id")
    apply_graph_health(
        graph,
        flow_set,
        tenant_id=tenant_id,
        catalog_ids=allowed,
        verdict_fn=lambda text: _line_verdict(text, tenant_id),
    )
    graph["tenant_id"] = tenant_id
    graph["catalog"] = catalog
    graph["default_flow_id"] = default_flow_id(prof.flow_prefix, scenario)
    graph["layout"] = read_layout(flow_id)
    return graph


class LayoutPut(BaseModel):
    nodes: list[dict[str, Any]] = Field(default_factory=list)


@router.get("/tenant/{tenant_id}/flow/{flow_id}/layout")
async def get_flow_layout(tenant_id: str, flow_id: str) -> dict[str, Any]:
    _require_enabled()
    _tenant_or_404(tenant_id)
    prof = get_tenant_profile(tenant_id)
    if prof is None:
        raise HTTPException(status_code=404, detail="unknown tenant")
    allowed = {row["id"] for row in tenant_catalog(prof, get_flow_set())}
    if flow_id not in allowed:
        raise HTTPException(status_code=404, detail="unknown flow_id")
    try:
        return read_layout(flow_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="unknown flow_id") from None


@router.put("/tenant/{tenant_id}/flow/{flow_id}/layout")
async def put_flow_layout(tenant_id: str, flow_id: str, body: LayoutPut) -> dict[str, Any]:
    _require_enabled()
    _tenant_or_404(tenant_id)
    prof = get_tenant_profile(tenant_id)
    if prof is None:
        raise HTTPException(status_code=404, detail="unknown tenant")
    allowed = {row["id"] for row in tenant_catalog(prof, get_flow_set())}
    if flow_id not in allowed:
        raise HTTPException(status_code=404, detail="unknown flow_id")
    try:
        return write_layout(flow_id, body.nodes)
    except ValueError:
        raise HTTPException(status_code=404, detail="unknown flow_id") from None


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
        request.app.state.flows = get_flow_set()
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
