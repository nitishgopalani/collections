"""Single-turn orchestration — full pipeline (Sprint 7)."""

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import UTC, date, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from app.config import get_settings, tenant_config
from app.engine.actions import make_async_action_runner
from app.engine.command_gen import generate
from app.engine.command_gen import CommandParseResult
from app.engine.command_gen import parse_and_validate_commands
from app.engine.compliance_handoff import sync_compliance_notes_on_persist
from app.engine.commitment_gate import (
    _slot_cost_class,
    commitment_gate,
    commitment_gate_enforce_enabled,
    flow_gate_class_map,
)
from app.engine.compose_renderer import render_compose, render_unrelated_redirect
from app.engine.dispute_breadth import sync_dispute_on_persist
from app.engine.echo_filter import detect_echo
from app.engine.evidence_scorer import has_question_shape, score_evidence
from app.engine.fragment_library import validate_compose
from app.engine.executor import ExecResult
from app.engine.executor import run_async as run_executor_async
from app.engine.followup import hydrate_followup_from_borrower, sync_followup_on_persist
from app.engine.gate import gate
from app.engine.hardship import sync_hardships_on_persist
from app.engine.identity_gate import apply_identity_entry_gate, defer_collection_flows
from app.engine.label_transition import run_label_transition
from app.engine.latency import StageTimer, TurnLatencyProfile
from app.engine.nlg import ResolvedReply, draft_reply_resolved, render_short_reask
from app.engine.respond_guard import ground_respond_text
from app.engine.priority import reorder
from app.engine.refusal_negotiation import sync_refusal_negotiation_on_persist
from app.engine.catalog import (
    build_scoped_catalog,
    filter_deflection_objections,
    tenant_flow_catalog,
)
from app.engine.retrieval import retrieve_flow_candidates
from app.engine.robustness import (
    FRUSTRATION_COUNT_KEY,
    FRUSTRATION_ESCALATION_DISPOSITION,
    LOCKED_SLOT_VALUES_KEY,
    mark_repair_escalation,
    record_agent_fault,
    record_outbound_context,
    track_frustration,
    track_slot_reask,
)
from app.engine.slot_validation import validate_commands
from app.clients.whatsapp import send_whatsapp
from app.engine.safety import (
    apply_call_window_to_state,
    apply_dnc_to_state,
    apply_safety_to_state,
    apply_third_party_flip_to_state,
    call_window_preempt,
    dnc_preempt,
    safety_preempt,
    third_party_flip_preempt,
)
from app.engine.tracker import apply, hydrate_from_borrower, new_conversation_state
from app.engines_p2.decision_overlay import apply_decision_overlay
from app.engines_p2.emotion import (
    apply_emotion_to_state,
    classify_emotion_from_turn,
    sync_emotion_on_persist,
)
from app.engines_p2.persona import apply_persona_to_state, sync_persona_on_persist
from app.engines_p2.recovery_prob import apply_recovery_to_state, sync_recovery_on_persist
from app.engines_p2.risk import apply_risk_to_state, sync_risk_on_persist
from app.engines_p2.trust import apply_trust_to_state, sync_trust_on_persist
from app.flows.loader import get_flow_set
from app.engine.tenant_profile import TenantRuntimeProfile, get_tenant_profile
from app.engine import scripted_coercions as _sc
from app.flows.manifest import MANIFEST_VERSION, load_reply_manifest
from app.flows.override_provider import NullOverrideProvider, OverrideProvider
from app.flows.overrides import OverrideValidationError, merge_response_overrides
from app.memory.audit import TurnAuditChain, build_turn_audit_record
from app.schemas.api import TurnRequest, TurnResponse
from app.schemas.command import Command
from app.schemas.flow import FlowSet
from app.schemas.manifest import ReplyManifest
from app.schemas.overrides import BrandOverridePack
from app.schemas.state import BorrowerRecord, ConversationState, Event, Frame
from app.ws.borrower_context import (
    apply_borrower_context_to_record,
    apply_borrower_context_to_state,
    normalize_borrower_context,
)
from app.ws.routing import FORCE_FLOW_ALIASES
from app.telemetry import annotate_turn_span, span, turn_trace
from app.engine.turn_decision_log import log_turn_decision

logger = logging.getLogger(__name__)

# Strong refs to detached transfer/whatsapp tasks so the loop can't GC them mid-flight.
_TRANSFER_TASKS: set[asyncio.Task[Any]] = set()
_WHATSAPP_TASKS: set[asyncio.Task[Any]] = set()


async def _send_whatsapp_bg(*, phone: str, name: str) -> None:
    """Fire the templated WhatsApp send detached from the turn (never raises)."""
    try:
        await send_whatsapp(phone=phone, name=name)
    except Exception:  # noqa: BLE001 — detached task must never surface an error
        logger.exception("whatsapp send failed name=%s", name)


# Warm transfer driver: poll cadence for GET /v1/transfer/{id}.
_TRANSFER_POLL_S = 1.0


def transfer_caller_id(slot_value: Any) -> str:
    """Caller ID for the agent leg: flow slot, else TRANSFER_CALLER_ID env.

    An empty caller ID makes the trunk dial out as "Anonymous", which the
    carrier rejects instantly (SIP 480) — the agent's phone never rings.
    Mirrors the consult path's CONSULT_CALLER_ID fallback.
    """
    value = str(slot_value or "").strip()
    if value:
        return value
    fallback = (
        os.getenv("TRANSFER_CALLER_ID", "") or get_settings().transfer_caller_id
    ).strip()
    if not fallback:
        logger.warning(
            "warm transfer: no caller_id slot and TRANSFER_CALLER_ID unset — "
            "agent leg will dial out anonymous and may be carrier-rejected"
        )
    return fallback


async def _drive_warm_transfer(
    hold_s: float,
    *,
    session_uuid: str,
    target: str,
    caller_id: str,
    reason: str,
    answer_budget_s: float,
    complete_delay_s: float,
    no_answer_reply: str = "",
    end_call_grace_ms: int = 700,
) -> None:
    """Run a full warm transfer against the ari-orchestrator. Never raises.

    Detached from the turn (background task) so the handoff line's TTS is sent
    immediately. Sequence:

    1. hold — let the "connecting you to a senior" line play before the agent
       can answer into the three-way;
    2. POST /v1/transfer by session_uuid — the orchestrator dials the agent;
       on answer the agent joins the customer's existing bridge (three-way
       with the AI, which stays up: its death would tear the whole call down);
    3. poll status until ``up`` / terminal / the answer budget runs out;
    4. ``up``      -> transfer/complete: the AI leg is dropped, customer and
       agent stay bridged (the connector session ends, the brain session dies
       with it — nothing more for us to do);
       no answer   -> transfer/cancel, push the tenant-configured spoken
       close (end_call + grace via the go-server), then let the connector
       hang up — no orchestrator customer hangup when the push succeeds;
       ``failed``  -> agent busy/declined: same spoken close;
       ``finished``/``cancelled`` -> the customer hung up mid-ring (the
       orchestrator already cleaned up) — nothing to do.
    """
    from app.clients import orchestrator
    from app.config import get_settings

    settings = get_settings()
    try:
        if hold_s > 0:
            await asyncio.sleep(hold_s)
        out = await asyncio.to_thread(
            orchestrator.warm_transfer,
            session_uuid=session_uuid,
            to=target,
            caller_id=caller_id,
            ring_budget_s=float(
                getattr(settings, "transfer_ring_budget_s", None)
                or settings.transfer_answer_budget_s
                or 30.0
            ),
        )
        transfer_id = str(out.get("id") or out.get("transfer_id") or "")
        if not transfer_id:
            logger.error(
                "warm transfer: no transfer_id session=%s response=%s", session_uuid, out
            )
            return
        logger.info(
            "warm transfer started session=%s transfer_id=%s target=%s reason=%s",
            session_uuid,
            transfer_id,
            target,
            reason,
        )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(answer_budget_s, _TRANSFER_POLL_S)
        status, st = "", {}
        while loop.time() < deadline:
            await asyncio.sleep(_TRANSFER_POLL_S)
            st = await asyncio.to_thread(
                orchestrator.transfer_status, transfer_id=transfer_id
            )
            status = str(st.get("status") or "")
            if orchestrator.status_matches(
                st, "up", "failed", "finished", "cancelled", "completed"
            ):
                break

        if orchestrator.status_matches(st, "up"):
            if complete_delay_s > 0:
                await asyncio.sleep(complete_delay_s)
            await asyncio.to_thread(
                orchestrator.transfer_complete, transfer_id=transfer_id
            )
            logger.info(
                "warm transfer completed session=%s transfer_id=%s (AI leg dropped)",
                session_uuid,
                transfer_id,
            )
            return
        if orchestrator.status_matches(st, "finished", "cancelled", "completed"):
            logger.info(
                "warm transfer already terminal session=%s transfer_id=%s status=%s",
                session_uuid,
                transfer_id,
                st.get("status", ""),
            )
            return

        # No answer within budget (or busy/declined). Cancel if still ringing,
        # then speak the tenant-configured close before teardown.
        if not orchestrator.status_matches(st, "failed"):
            await asyncio.to_thread(
                orchestrator.transfer_cancel, transfer_id=transfer_id
            )
        reply = (no_answer_reply or "").strip()
        if reply:
            from app.ws import outbound_push

            pushed = await outbound_push.push_unsolicited_reply(
                session_uuid,
                reply,
                disposition="TRANSFER_NO_ANSWER",
                end_call=True,
                end_call_delay_ms=end_call_grace_ms,
                turn_id_prefix="transfer-no-answer",
            )
            if pushed:
                logger.info(
                    "warm transfer no-answer spoken close pushed session=%s "
                    "transfer_id=%s status=%s grace_ms=%s",
                    session_uuid,
                    transfer_id,
                    status or "no-answer",
                    end_call_grace_ms,
                )
                return
        customer = str(st.get("customer_channel_id") or "")
        logger.warning(
            "warm transfer failed session=%s transfer_id=%s status=%s "
            "(spoken close unavailable; hanging up customer leg %s)",
            session_uuid,
            transfer_id,
            status or "no-answer",
            customer or "?",
        )
        if customer:
            await asyncio.to_thread(orchestrator.hangup, channel_id=customer)
    except Exception:  # noqa: BLE001 — detached task must never surface an error
        logger.exception("warm transfer driver failed session=%s", session_uuid)

_REPLY_MANIFEST: ReplyManifest = load_reply_manifest()

# ---------------------------------------------------------------------------
# Scripted-tenant coercions (profile-driven). Constants live in
# app/tenants/<tenant>.yml; shared inability regex in scripted_coercions.
# Thin `_coerce_sot_*` wrappers preserve test imports with zero behaviour change.
# ---------------------------------------------------------------------------

def _awaiting_collect_slot(state: ConversationState, flows: FlowSet) -> str:
    """Slot the active (paused) flow step is waiting to collect, or "" if none."""
    if not state.flow_stack:
        return ""
    frame = state.flow_stack[-1]
    flow = flows.flows.get(frame.flow)
    if flow is None or frame.step_index >= len(flow.steps):
        return ""
    return flow.steps[frame.step_index].collect or ""


def _sot_profile() -> TenantRuntimeProfile:
    profile = get_tenant_profile("salary_on_time")
    if profile is None:
        raise RuntimeError("salary_on_time tenant profile missing under app/tenants/")
    return profile


# DEBT-018: resolve a profile.test_borrower_factory name (e.g. "hardcoded_test_borrower")
# to the callable in app.memory.test_borrower. Keeps the YAML serialisable and the
# import lazy (test-mode only).
_TEST_BORROWER_FACTORY_CACHE: dict[str, Any] = {}


def _resolve_test_borrower_factory(factory_name: str) -> Any:
    if not factory_name:
        raise ValueError("test_borrower_factory is empty on this profile")
    cached = _TEST_BORROWER_FACTORY_CACHE.get(factory_name)
    if cached is not None:
        return cached
    from app.memory import test_borrower as _tb

    factory = getattr(_tb, factory_name, None)
    if factory is None or not callable(factory):
        raise ValueError(
            f"test_borrower_factory {factory_name!r} not found in app.memory.test_borrower"
        )
    _TEST_BORROWER_FACTORY_CACHE[factory_name] = factory
    return factory


# Back-compat aliases used by unit/golden tests that call coercers directly.
_SOT_INABILITY_RE = _sc.INABILITY_RE


def _sot_transcript_blank(transcript: str) -> bool:
    return _sc.transcript_blank(transcript)


def _is_sot_main_ladder_flow(flow_name: str) -> bool:
    return _sc.is_main_ladder_flow(flow_name, _sot_profile())


def _prune_spurious_sot_objection_stack(state: ConversationState) -> ConversationState:
    return _sc.prune_spurious_objection_stack(state, _sot_profile())


def _sanitize_sot_commands_for_blank_transcript(commands: list[Command]) -> list[Command]:
    return _sc.sanitize_blank_transcript_commands(commands)


def _sot_dispute_flow(transcript: str) -> str | None:
    return _sc.dispute_flow(transcript, _sot_profile())


def _coerce_sot_dispute(
    commands: list[Command], transcript: str, *, on_rails: bool
) -> tuple[list[Command], bool]:
    return _sc.coerce_dispute(
        commands, transcript, on_rails=on_rails, profile=_sot_profile()
    )


def _coerce_sot_push_willing(
    commands: list[Command], awaiting_slot: str, transcript: str
) -> tuple[list[Command], bool]:
    return _sc.coerce_push_willing(
        commands, awaiting_slot, transcript, profile=_sot_profile()
    )


def _coerce_sot_payment_refusal(
    commands: list[Command], awaiting_slot: str, transcript: str
) -> tuple[list[Command], bool]:
    cmds, fired, _via, _cls = _sc.coerce_payment_refusal(
        commands, awaiting_slot, transcript, profile=_sot_profile()
    )
    return cmds, fired


def _coerce_sot_identity(
    commands: list[Command], awaiting_slot: str, transcript: str
) -> list[Command]:
    return _sc.coerce_identity(
        commands, awaiting_slot, transcript, profile=_sot_profile()
    )


def _coerce_sot_commit_reversal(
    commands: list[Command], awaiting_slot: str, transcript: str
) -> tuple[list[Command], bool]:
    return _sc.coerce_commit_reversal(
        commands, awaiting_slot, transcript, profile=_sot_profile()
    )


def _coerce_sot_confirm(
    commands: list[Command], awaiting_slot: str, transcript: str
) -> list[Command]:
    return _sc.coerce_confirm(
        commands, awaiting_slot, transcript, profile=_sot_profile()
    )


def _coerce_sot_link_received(
    commands: list[Command], awaiting_slot: str, transcript: str
) -> list[Command]:
    return _sc.coerce_link_received(
        commands, awaiting_slot, transcript, profile=_sot_profile()
    )


def _clarify_if_ambiguous(
    commands: list[Command],
    candidate_flows: list[dict[str, Any]],
    *,
    delta: float,
) -> tuple[list[Command], bool]:
    """F6: ask to clarify instead of guessing when the top-2 flow candidates ~tie.

    Only fires when the LLM's sole actionable command is a single start_flow (no
    set_slot alongside it) and the two highest-scoring candidates are different
    flows scoring within ``delta`` of each other. Returns (commands, fired).
    Skip when candidates carry no numeric scores (Tier-2 catalog mode).
    """
    starts = [c for c in commands if c.command == "start_flow"]
    if len(starts) != 1 or any(c.command == "set_slot" for c in commands):
        return commands, False
    if not any(c.get("score") is not None for c in candidate_flows):
        return commands, False
    scored = sorted(
        (
            (str(c.get("name", "")), float(c.get("score") or 0.0))
            for c in candidate_flows
            if c.get("name")
        ),
        key=lambda pair: pair[1],
        reverse=True,
    )
    if len(scored) < 2:
        return commands, False
    (top_name, top_score), (_, second_score) = scored[0], scored[1]
    if scored[0][0] == scored[1][0] or (top_score - second_score) > delta:
        return commands, False
    return [Command(command="clarify")], True


def _merge_pinned_flow_candidates(
    candidate_flows: list[dict[str, Any]],
    pinned_names: list[str],
    flows: FlowSet,
) -> list[dict[str, Any]]:
    """Layer 0: guarantee critical flows are always start_flow candidates.

    Dense KB retrieval has known recall/negation weaknesses (NevIR), so a borrower
    asking "kaise pay karun" can fail to surface ``sot_obj_link_request`` while an
    opposite-intent flow ranks higher. We append the pinned flows — with their local
    description and no KB score — so the LLM can always route to them. Pinned entries
    carry ``score=None`` so the confidence floor treats them as exempt (they were not
    retrieval-ranked). Already-present candidates are left untouched.
    """
    if not pinned_names:
        return candidate_flows
    present = {str(c.get("name", "")) for c in candidate_flows}
    merged = list(candidate_flows)
    for name in pinned_names:
        if name in present:
            continue
        flow = flows.flows.get(name)
        if flow is None:
            continue
        merged.append({"name": name, "description": flow.description, "score": None})
    return merged


def _suppress_low_confidence_flow_jumps(
    commands: list[Command],
    candidate_flows: list[dict[str, Any]],
    *,
    pinned_names: frozenset[str],
    floor: float,
) -> tuple[list[Command], bool]:
    """Layer 3: drop a start_flow backed only by a weak KB retrieval score.

    Applied while the borrower is answering a scripted collect question. A jump whose
    chosen flow scored below ``floor`` is a likely false digression (dense retrieval
    ranks near-miss / opposite-intent flows highly), so we suppress it and let a
    co-emitted set_slot (the borrower's actual answer) or a re-ask clarify handle the
    turn. Flows with no numeric KB score (pinned or deterministically coerced) are
    exempt, as are names in ``pinned_names``.
    """
    if floor <= 0:
        return commands, False
    scores: dict[str, Any] = {
        str(c.get("name", "")): c.get("score") for c in candidate_flows
    }
    kept: list[Command] = []
    suppressed = False
    for cmd in commands:
        if cmd.command == "start_flow":
            name = str(cmd.flow or "")
            score = scores.get(name)
            if name not in pinned_names and score is not None and float(score) < floor:
                suppressed = True
                continue
        kept.append(cmd)
    if suppressed and not any(
        c.command in {"start_flow", "set_slot", "cancel_flow"} for c in kept
    ):
        kept.append(Command(command="clarify"))
    return kept, suppressed


DISPUTE_EVIDENCE_KEY = "_dispute_evidence"


def _dispute_evidence_this_turn(
    transcript: str,
    proposed_commands: list[Command],
    dispute_flows: frozenset[str],
    *,
    profile: TenantRuntimeProfile | None = None,
) -> str | None:
    """Which high-stakes dispute theme the borrower expressed this turn, if any.

    Evidence must reflect what the borrower actually said — NOT merely that a dispute
    flow was a retrieval/pinned candidate (a pinned dispute flow is a candidate on every
    turn, so candidate-presence would fire false evidence). The two valid signals are:
    the deterministic dispute matcher recognizes the utterance, or the LLM's *proposed*
    commands (pre-suppression) include a start_flow into a dispute flow — i.e. the model
    read this utterance as that dispute even if the floor later suppressed it.
    """
    det = (
        _sc.dispute_flow(transcript, profile)
        if profile is not None
        else _sot_dispute_flow(transcript)
    )
    if det in dispute_flows:
        return det
    for cmd in proposed_commands:
        if cmd.command == "start_flow" and str(cmd.flow or "") in dispute_flows:
            return str(cmd.flow)
    return None


def _accumulate_dispute_evidence(
    state: ConversationState,
    commands: list[Command],
    evidence_theme: str | None,
    *,
    bar: int,
) -> tuple[ConversationState, list[Command], str | None]:
    """Cross-turn evidence accumulator for high-stakes disputes.

    Evidence = deterministic matcher OR LLM-proposed ``start_flow`` into a dispute
    theme (see :func:`_dispute_evidence_this_turn`). Once a theme reaches ``bar``
    corroborating turns we force its start_flow. Scoped to dispute themes only.
    Returns (state, commands, forced_flow_or_None).
    """
    updated = state.model_copy(deep=True)
    slots = dict(updated.slots)
    counts: dict[str, int] = dict(slots.get(DISPUTE_EVIDENCE_KEY) or {})

    forced: str | None = None
    if evidence_theme:
        already_routing = any(
            cmd.command == "start_flow" and str(cmd.flow or "") == evidence_theme
            for cmd in commands
        )
        if already_routing:
            # Deterministic coercion or an allowed jump is already routing it — no need
            # to accumulate; clear the counter so a later unrelated turn starts fresh.
            counts[evidence_theme] = 0
        else:
            counts[evidence_theme] = int(counts.get(evidence_theme, 0)) + 1
            if bar > 0 and counts[evidence_theme] >= bar:
                forced = evidence_theme
                counts[evidence_theme] = 0
                commands = [Command(command="start_flow", flow=evidence_theme)]

    slots[DISPUTE_EVIDENCE_KEY] = counts
    updated.slots = slots
    return updated, commands, forced


def _resolve_effective_flows(
    flows: FlowSet,
    brand_pack: BrandOverridePack | None,
) -> tuple[FlowSet, bool, str | None]:
    """Merge brand overrides onto platform responses; degrade on validation failure."""
    if brand_pack is None:
        return flows, False, None
    try:
        effective = merge_response_overrides(flows.responses, brand_pack, _REPLY_MANIFEST)
        flows_eff = FlowSet(flows=flows.flows, responses=effective)
        return flows_eff, False, None
    except OverrideValidationError as exc:
        reason = "; ".join(f"{error.reply_id}:{error.code}" for error in exc.errors)
        logger.warning("Brand override pack rejected: %s", reason)
        return flows, True, reason


def gate_clock_from_state(
    state: ConversationState,
    tenant_cfg: Any,
) -> datetime | None:
    """Use call_date at 10:00 local when set — stabilizes gate window for replay/tests."""
    raw = state.slots.get("call_date") or state.slots.get("today")
    if not isinstance(raw, str) or len(raw) < 10:
        return None
    try:
        call_day = date.fromisoformat(raw[:10])
        tz = ZoneInfo(tenant_cfg.call_window_timezone)
        return datetime(call_day.year, call_day.month, call_day.day, 10, 0, tzinfo=tz)
    except ValueError:
        return None


def sync_borrower_from_state(borrower: BorrowerRecord, state: ConversationState) -> BorrowerRecord:
    updated = borrower.model_copy(deep=True)
    loan = dict(updated.loan)
    for key in ("amount_due", "dpd", "bucket"):
        if key in state.slots:
            loan[key] = state.slots[key]
    updated.loan = loan
    if "compliance_flags" in state.slots:
        updated.compliance_flags = dict(state.slots["compliance_flags"])
    if state.slots.get("identity_ok"):
        identity = dict(updated.identity)
        identity["identity_ok"] = True
        updated.identity = identity
    updated = sync_trust_on_persist(updated, trigger="turn_persist")
    updated = sync_risk_on_persist(updated, trigger="turn_persist")
    return updated


def process_outbound_reply(
    draft_reply: str,
    state: ConversationState,
    request: TurnRequest,
    *,
    candidate_flows: list[dict[str, Any]] | None = None,
    commands: list[dict[str, Any]] | None = None,
    actions_called: list[str] | None = None,
    safety_reason: str | None = None,
    latency: TurnLatencyProfile | None = None,
    llm_calls: int = 0,
    now: datetime | None = None,
    resolved: ResolvedReply | None = None,
    manifest_version: str | None = None,
    brand_pack: BrandOverridePack | None = None,
    pack_rejected: bool = False,
    pack_rejected_reason: str | None = None,
) -> tuple[str, ConversationState, bool, TurnAuditChain]:
    """Apply compliance gate and build audit chain."""
    tenant_cfg = tenant_config(request.tenant_id)
    gate_result = gate(
        draft_reply,
        state,
        tenant_cfg,
        inbound_transcript=request.transcript,
        now=now or gate_clock_from_state(state, tenant_cfg),
    )

    updated = state
    transfer = bool(state.slots.get("transfer_to_human")) or gate_result.transfer_to_human

    audit_id = str(uuid.uuid4())
    latency_data: dict[str, float | dict[str, float]] = (
        latency.to_dict() if latency is not None else {}
    )
    stages_raw = latency_data.get("stages", {})
    stages: dict[str, float] = stages_raw if isinstance(stages_raw, dict) else {}
    chain = TurnAuditChain(
        audit_id=audit_id,
        call_id=request.call_id,
        borrower_id=request.borrower_id,
        tenant_id=request.tenant_id,
        ts=datetime.now(UTC).isoformat(),
        candidate_flows=candidate_flows or [],
        commands=commands or [],
        actions_called=actions_called or [],
        safety_preempted=safety_reason is not None,
        safety_reason=safety_reason,
        gate_verdict=gate_result.verdict,
        gate_level=gate_result.level,
        gate_reason=gate_result.reason,
        gate_warnings=list(gate_result.warnings or []),
        final_reply=gate_result.text,
        transfer_to_human=transfer or gate_result.transfer_to_human,
        latency_ms=stages,
        engine_internal_ms=float(cast(float, latency_data.get("engine_internal_ms", 0.0))),
        external_ms=float(cast(float, latency_data.get("external_ms", 0.0))),
        llm_calls=llm_calls,
        reply_id=resolved.reply_id if resolved else None,
        variant_index=resolved.variant_index if resolved else None,
        language=resolved.language if resolved else None,
        tone_register=resolved.tone_register if resolved else None,
        agent_id=request.agent_id,
        pack_id=brand_pack.pack_id if brand_pack is not None else request.pack_id,
        manifest_version=manifest_version,
        pack_rejected=pack_rejected,
        pack_rejected_reason=pack_rejected_reason,
    )
    return gate_result.text, updated, chain.transfer_to_human, chain


def safety_check_transcript(
    request: TurnRequest,
    state: ConversationState,
) -> tuple[ConversationState, str | None]:
    """Run safety pre-empt; return updated state and optional early reply text."""
    tenant_cfg = tenant_config(request.tenant_id)
    from app.engine.tenant_profile import get_tenant_profile

    profile = get_tenant_profile(request.tenant_id)
    safety = safety_preempt(
        request.transcript,
        state,
        tenant_cfg,
        emotion_label=state.slots.get("emotion"),
        emotion_intensity=state.slots.get("emotion_intensity"),
        profile=profile,
    )
    if safety is None:
        return state, None
    updated = apply_safety_to_state(state, safety)
    return updated, safety.reply_text


def dnc_check_transcript(
    request: TurnRequest,
    state: ConversationState,
) -> tuple[ConversationState, str | None, bool]:
    """W1-C C2: run DNC/opt-out policy pre-empt. Returns (state, reply_text, is_dnc).

    ``is_dnc`` is True when the DNC lane fired — the caller asked us to stop
    calling. The early-exit speaks the non-committal policy_stop_calls wording,
    tags disposition=dnc_requested, and graceful ENDs (outcome 7). Does NOT
    promise dialer suppression (W4 work).
    """
    tenant_cfg = tenant_config(request.tenant_id)
    from app.engine.tenant_profile import get_tenant_profile

    profile = get_tenant_profile(request.tenant_id)
    dnc = dnc_preempt(request.transcript, state, tenant_cfg, profile=profile)
    if dnc is None:
        return state, None, False
    updated = apply_dnc_to_state(state, dnc)
    return updated, dnc.reply_text, True


def call_window_check_transcript(
    request: TurnRequest,
    state: ConversationState,
) -> tuple[ConversationState, str | None, bool]:
    """W1-C C3: mid-call window-cross policy pre-empt. Returns (state, reply, is_close).

    ``is_close`` is True when the call has crossed the configured window
    boundary mid-conversation. The early-exit speaks the scripted polite close,
    tags disposition=call_window_closed, and graceful ENDs (outcome 7) —
    never a mid-call silent_reply.
    """
    tenant_cfg = tenant_config(request.tenant_id)
    from app.engine.tenant_profile import get_tenant_profile

    profile = get_tenant_profile(request.tenant_id)
    close = call_window_preempt(state, tenant_cfg, profile=profile)
    if close is None:
        return state, None, False
    updated = apply_call_window_to_state(state, close)
    return updated, close.reply_text, True


def third_party_flip_check_transcript(
    request: TurnRequest,
    state: ConversationState,
) -> tuple[ConversationState, str | None, bool, str]:
    """W1-C C4: third-party / speaker-flip policy pre-empt.

    Returns (state, reply, is_flip, mode). ``is_flip`` is True when the
    speaker-flip lane fired. ``mode`` is the DPDP posture that fired
    (``strict`` / ``relaxed`` / ``open_tier``). The early-exit speaks the
    third-party script + callback capture, revokes identity_current, locks
    disclosure (strict) or downgrades to generic-only (relaxed), and tags
    disposition=THIRD_PARTY_FLAGGED. Strict ENDs (outcome 7); relaxed may
    continue. ALWAYS-ON: third_party_suspected + identity_current transition
    logged regardless of mode.
    """
    tenant_cfg = tenant_config(request.tenant_id)
    from app.engine.tenant_profile import get_tenant_profile

    profile = get_tenant_profile(request.tenant_id)
    flip = third_party_flip_preempt(
        request.transcript, state, tenant_cfg, profile=profile
    )
    if flip is None:
        return state, None, False, ""
    updated = apply_third_party_flip_to_state(state, flip)
    mode = flip.reason.rsplit(":", 1)[-1] if ":" in flip.reason else ""
    return updated, flip.reply_text, True, mode


async def _persist_turn(
    memory: Any,
    state: ConversationState,
    borrower: BorrowerRecord,
    request: TurnRequest,
    audit_chain: TurnAuditChain,
) -> str:
    borrower = sync_borrower_from_state(borrower, state)
    borrower = sync_hardships_on_persist(borrower, state)
    borrower = sync_compliance_notes_on_persist(borrower, state)
    borrower = sync_followup_on_persist(borrower, state)
    borrower = sync_refusal_negotiation_on_persist(borrower, state)
    borrower = sync_dispute_on_persist(borrower, state)
    borrower = sync_risk_on_persist(borrower, trigger="turn_persist")
    borrower = sync_emotion_on_persist(borrower, state=state, trigger="turn_persist")
    borrower = sync_persona_on_persist(borrower, state=state, trigger="turn_persist")
    borrower = sync_recovery_on_persist(borrower, state=state, trigger="turn_persist")
    audit_chain.recovery = dict(borrower.recovery)
    cleaned = state.model_copy(deep=True)
    slots = dict(cleaned.slots)
    slots.pop("opt_out_ack_this_turn", None)
    slots.pop("compliance_note_pending", None)
    for key in (
        "ptp_record_pending",
        "broken_ptp_record_pending",
        "payment_link_record_pending",
        "callback_pending",
        "call_context_note_pending",
        "refusal_record_pending",
        "negotiation_packet_pending",
        "grievance_record_pending",
        "dispute_record_pending",
    ):
        slots.pop(key, None)
    cleaned.slots = slots
    await memory.save_state(cleaned)
    await memory.save_borrower(borrower)
    audit_record = build_turn_audit_record(audit_chain)
    await memory.append_audit(
        audit_record.event,
        call_id=request.call_id,
        borrower_id=request.borrower_id,
        tenant_id=request.tenant_id,
    )
    return audit_record.audit_id


async def _stash_brand_pack(
    state: ConversationState,
    override_provider: OverrideProvider,
    request: TurnRequest,
) -> BrandOverridePack | None:
    pack = await override_provider.get_pack(
        agent_id=request.agent_id,
        pack_id=request.pack_id,
    )
    if pack is not None:
        state.slots["brand_override_pack_id"] = pack.pack_id
        state.slots["brand_override_agent_id"] = pack.agent_id
    return pack


def _resolve_turn_voice(state: ConversationState, tenant_id: str) -> tuple[str | None, str | None, float | None]:
    """Resolve (voice_id, tts_model, tts_pace) for a preempt close reply.

    Mirrors the normal-path voice resolution (turn.py L2056-2071): profile
    defaults populate slots when absent; slot overrides win. Used by the
    DEBT-039 preempt close-reply path so the spoken close uses the same
    scenario voice as the rest of the call (e.g. simran for paisalo predue).
    """
    from app.engine.tenant_profile import get_tenant_profile

    profile = get_tenant_profile(tenant_id)
    if profile is not None:
        if profile.voice_id and not state.slots.get("voice_id"):
            state.slots.setdefault("voice_id", profile.voice_id)
        if profile.tts_model and not state.slots.get("tts_model"):
            state.slots.setdefault("tts_model", profile.tts_model)
        if profile.tts_pace is not None and state.slots.get("tts_pace") is None:
            state.slots["tts_pace"] = profile.tts_pace
    voice_id = state.slots.get("voice_id")
    tts_model = state.slots.get("tts_model")
    tts_pace_raw = state.slots.get("tts_pace")
    tts_pace: float | None = None
    if tts_pace_raw is not None and tts_pace_raw != "":
        try:
            tts_pace = float(tts_pace_raw)
        except (TypeError, ValueError):
            tts_pace = None
    return (
        str(voice_id) if voice_id else None,
        str(tts_model) if tts_model else None,
        tts_pace,
    )


def _interpolate_close_reply(text: str, state: ConversationState) -> str:
    """DEBT-039: interpolate {customer_name} into a preempt close reply.

    third_party_close uses {customer_name} so the spoken close addresses the
    borrower by name even after identity is revoked. Resolved from
    state.slots["customer_name"] (hydrated at session_start from borrower
    context). Missing slot → fall back to a respectful generic ("आप") so the
    close never speaks a literal "{customer_name}".
    """
    if not text:
        return text
    name = str(state.slots.get("customer_name") or "").strip()
    if name:
        return text.replace("{customer_name}", name)
    return text.replace("{customer_name}", "आप")


async def _emit_preempt_close(
    on_gated_reply: Any | None,
    reply_text: str,
    state: ConversationState,
    request: TurnRequest,
) -> None:
    """DEBT-039: emit ChunkMessages for a preempt close reply before end_call.

    Reuses the proven speak-then-close mechanics from the C0 apology path:
    the normal-path ``on_gated_reply`` callback (``_emit_gated_chunks`` in
    handler.py) sends ``ChunkMessage`` frames to the go-server, which
    synthesizes TTS and egresses audio before ``OnReplyDone(endCall=true)``
    finalizes the call. Without this call the go-server receives
    ``DoneMessage(end_call=true)`` with zero chunks → ``tts_ms=0`` silent
    hangup (the DEBT-039 root cause observed in sessions 9aaf5dd2 + a58b6077).
    """
    if on_gated_reply is None or not (reply_text or "").strip():
        return
    voice_id, tts_model, tts_pace = _resolve_turn_voice(state, request.tenant_id)
    await on_gated_reply(
        reply_text,
        voice_id=voice_id,
        tts_model=tts_model,
        tts_pace=tts_pace,
    )


async def _run_safety_early_exit(
    request: TurnRequest,
    state: ConversationState,
    borrower: BorrowerRecord,
    memory: Any,
    latency: TurnLatencyProfile,
    turn_span: Any,
    llm_calls: int,
    safety_reply: str,
    *,
    brand_pack: BrandOverridePack | None = None,
    on_gated_reply: Any | None = None,
) -> TurnResponse:
    state = apply(
        state,
        [
            Event(
                ts=datetime.now(UTC).isoformat(),
                kind="safety_preempt",
                data={"transcript_len": len(request.transcript)},
            ),
        ],
    )
    state.attempts += 1

    safety_reply = _interpolate_close_reply(safety_reply, state)
    reply_text, state, transfer, audit_chain = process_outbound_reply(
        safety_reply,
        state,
        request,
        safety_reason="safety_preempt",
        latency=latency,
        llm_calls=llm_calls,
        manifest_version=MANIFEST_VERSION,
        brand_pack=brand_pack,
    )

    log_turn_decision(
        session_id=request.call_id,
        transcript=request.transcript,
        borrower=borrower,
        kb_candidates=[],
        commands=[],
        rejected_slots=[],
        state=state,
        reply_id=None,
        gate_verdict=audit_chain.gate_verdict,
        gate_reason=audit_chain.gate_reason,
        draft_reply=safety_reply,
        final_reply=reply_text,
    )

    # W2-1: persist spoken reply + borrower transcript for the next turn's
    # echo filter + evidence scorer (telemetry-only slots, written after gate).
    state.slots["last_spoken_reply"] = reply_text or ""
    state.slots["_last_borrower_transcript"] = request.transcript or ""

    with StageTimer(latency, "persist"):
        audit_id = await _persist_turn(memory, state, borrower, request, audit_chain)

    annotate_turn_span(turn_span, chain=audit_chain, latency=latency, llm_calls=llm_calls)
    # DEBT-039: speak the vulnerability close reply before returning. The
    # safety lane transfers to a human (end_call=False) so the caller stays
    # on the line and hears the close before the handoff.
    await _emit_preempt_close(on_gated_reply, reply_text, state, request)
    # W1-C C1 (policy interrupt — vulnerability lane): policy-lane entries land
    # in outcome 5 (transfer to human specialist) with a named disposition. The
    # safety_preempt already suppresses dunning + suspends recovery + transfers
    # to a human; the disposition makes the outcome greppable in audit/telemetry.
    return TurnResponse(
        reply_text=reply_text,
        end_call=False,
        transfer_to_human=transfer,
        actions_executed=[],
        disposition="VULNERABLE_FLAGGED",
        state_version=state.version,
        audit_id=audit_id,
    )


async def _run_dnc_early_exit(
    request: TurnRequest,
    state: ConversationState,
    borrower: BorrowerRecord,
    memory: Any,
    latency: TurnLatencyProfile,
    turn_span: Any,
    llm_calls: int,
    dnc_reply: str,
    *,
    brand_pack: BrandOverridePack | None = None,
    on_gated_reply: Any | None = None,
) -> TurnResponse:
    """W1-C C2 (DNC/opt-out capture, policy interrupt): terminal early-exit.

    The caller asked us to stop calling. Speak the non-committal
    ``policy_stop_calls`` wording (request recorded; final confirmation from
    the brand — does NOT promise dialer suppression until W4), tag
    ``disposition=dnc_requested``, and graceful END (outcome 7). The scorer
    never runs on a DNC cue.
    """
    state = apply(
        state,
        [
            Event(
                ts=datetime.now(UTC).isoformat(),
                kind="dnc_preempt",
                data={"transcript_len": len(request.transcript)},
            ),
        ],
    )
    state.attempts += 1

    dnc_reply = _interpolate_close_reply(dnc_reply, state)
    reply_text, state, transfer, audit_chain = process_outbound_reply(
        dnc_reply,
        state,
        request,
        safety_reason="dnc_preempt",
        latency=latency,
        llm_calls=llm_calls,
        manifest_version=MANIFEST_VERSION,
        brand_pack=brand_pack,
    )

    log_turn_decision(
        session_id=request.call_id,
        transcript=request.transcript,
        borrower=borrower,
        kb_candidates=[],
        commands=[],
        rejected_slots=[],
        state=state,
        reply_id=None,
        gate_verdict=audit_chain.gate_verdict,
        gate_reason=audit_chain.gate_reason,
        draft_reply=dnc_reply,
        final_reply=reply_text,
    )

    # W2-1: persist spoken reply + borrower transcript for the next turn's
    # echo filter + evidence scorer (telemetry-only slots, written after gate).
    state.slots["last_spoken_reply"] = reply_text or ""
    state.slots["_last_borrower_transcript"] = request.transcript or ""

    with StageTimer(latency, "persist"):
        audit_id = await _persist_turn(memory, state, borrower, request, audit_chain)

    annotate_turn_span(turn_span, chain=audit_chain, latency=latency, llm_calls=llm_calls)
    # DEBT-039: speak the DNC ack before end_call so the caller hears the
    # request-recorded confirmation (tts_ms>0) instead of a silent hangup.
    await _emit_preempt_close(on_gated_reply, reply_text, state, request)
    return TurnResponse(
        reply_text=reply_text,
        end_call=True,
        transfer_to_human=False,
        actions_executed=[],
        disposition="dnc_requested",
        state_version=state.version,
        audit_id=audit_id,
    )


async def _run_call_window_early_exit(
    request: TurnRequest,
    state: ConversationState,
    borrower: BorrowerRecord,
    memory: Any,
    latency: TurnLatencyProfile,
    turn_span: Any,
    llm_calls: int,
    cw_reply: str,
    *,
    brand_pack: BrandOverridePack | None = None,
    on_gated_reply: Any | None = None,
) -> TurnResponse:
    """W1-C C3 (call-window close-out, policy interrupt): terminal early-exit.

    An answered call has crossed the configured window boundary
    mid-conversation. Speak the scripted polite close, tag
    ``disposition=call_window_closed``, and graceful END (outcome 7). Never a
    mid-call silent_reply — the gate's ``outside_call_window`` block is
    correct for a fresh call (do not answer) but wrong mid-call.
    """
    state = apply(
        state,
        [
            Event(
                ts=datetime.now(UTC).isoformat(),
                kind="call_window_preempt",
                data={"attempts": state.attempts},
            ),
        ],
    )
    state.attempts += 1

    cw_reply = _interpolate_close_reply(cw_reply, state)
    reply_text, state, transfer, audit_chain = process_outbound_reply(
        cw_reply,
        state,
        request,
        safety_reason="call_window_preempt",
        latency=latency,
        llm_calls=llm_calls,
        manifest_version=MANIFEST_VERSION,
        brand_pack=brand_pack,
    )

    log_turn_decision(
        session_id=request.call_id,
        transcript=request.transcript,
        borrower=borrower,
        kb_candidates=[],
        commands=[],
        rejected_slots=[],
        state=state,
        reply_id=None,
        gate_verdict=audit_chain.gate_verdict,
        gate_reason=audit_chain.gate_reason,
        draft_reply=cw_reply,
        final_reply=reply_text,
    )

    # W2-1: persist spoken reply + borrower transcript for the next turn's
    # echo filter + evidence scorer (telemetry-only slots, written after gate).
    state.slots["last_spoken_reply"] = reply_text or ""
    state.slots["_last_borrower_transcript"] = request.transcript or ""

    with StageTimer(latency, "persist"):
        audit_id = await _persist_turn(memory, state, borrower, request, audit_chain)

    annotate_turn_span(turn_span, chain=audit_chain, latency=latency, llm_calls=llm_calls)
    # DEBT-039: speak the window close before end_call (tts_ms>0).
    await _emit_preempt_close(on_gated_reply, reply_text, state, request)
    return TurnResponse(
        reply_text=reply_text,
        end_call=True,
        transfer_to_human=False,
        actions_executed=[],
        disposition="call_window_closed",
        state_version=state.version,
        audit_id=audit_id,
    )


async def _run_third_party_flip_early_exit(
    request: TurnRequest,
    state: ConversationState,
    borrower: BorrowerRecord,
    memory: Any,
    latency: TurnLatencyProfile,
    turn_span: Any,
    llm_calls: int,
    flip_reply: str,
    flip_mode: str,
    *,
    brand_pack: BrandOverridePack | None = None,
    on_gated_reply: Any | None = None,
) -> TurnResponse:
    """W1-C C4 (third-party / speaker-flip guard, policy interrupt): early-exit.

    A different speaker has joined or taken over the call mid-conversation.
    ``identity_current`` is revoked (``identity_ok=False``), disclosure is
    LOCKed (strict) or downgraded to generic-only (relaxed), the third-party
    script + callback capture is spoken, and the turn is tagged
    ``disposition=THIRD_PARTY_FLAGGED``. Strict mode ENDs (outcome 7);
    relaxed mode may continue (``end_call=False``). Open-tier (lab) logs the
    suspicion without locking or ending.

    ALWAYS-ON regardless of mode: ``third_party_suspected=true`` and the
    ``identity_current`` transition are logged (audit trail not configurable).
    """
    # Always log the audit-trail events (not configurable) — even in open-tier.
    logger.info(
        "third_party_suspected=true call_id=%s tenant_id=%s mode=%s transcript_len=%d",
        request.call_id,
        request.tenant_id,
        flip_mode or "unknown",
        len(request.transcript),
    )
    logger.info(
        "identity_current transition: revoked call_id=%s tenant_id=%s reason=third_party_flip",
        request.call_id,
        request.tenant_id,
    )

    state = apply(
        state,
        [
            Event(
                ts=datetime.now(UTC).isoformat(),
                kind="third_party_flip_preempt",
                data={"mode": flip_mode, "transcript_len": len(request.transcript)},
            ),
        ],
    )
    state.attempts += 1

    # Open-tier (lab): no reply, no lock, no end — just the audit log above.
    if flip_mode == "open_tier":
        annotate_turn_span(turn_span, chain=TurnAuditChain(
            audit_id=str(uuid.uuid4()),
            call_id=request.call_id,
            borrower_id=request.borrower_id,
            tenant_id=request.tenant_id,
            ts=datetime.now(UTC).isoformat(),
            safety_preempted=True,
            safety_reason="third_party_flip_preempt:open_tier",
        ), latency=latency, llm_calls=llm_calls)
        return TurnResponse(
            reply_text="",
            end_call=False,
            transfer_to_human=False,
            actions_executed=[],
            disposition="THIRD_PARTY_FLAGGED",
            state_version=state.version,
            audit_id="",
        )

    flip_reply = _interpolate_close_reply(flip_reply, state)
    reply_text, state, transfer, audit_chain = process_outbound_reply(
        flip_reply,
        state,
        request,
        safety_reason="third_party_flip_preempt",
        latency=latency,
        llm_calls=llm_calls,
        manifest_version=MANIFEST_VERSION,
        brand_pack=brand_pack,
    )

    log_turn_decision(
        session_id=request.call_id,
        transcript=request.transcript,
        borrower=borrower,
        kb_candidates=[],
        commands=[],
        rejected_slots=[],
        state=state,
        reply_id=None,
        gate_verdict=audit_chain.gate_verdict,
        gate_reason=audit_chain.gate_reason,
        draft_reply=flip_reply,
        final_reply=reply_text,
    )

    # W2-1: persist spoken reply + borrower transcript for the next turn's
    # echo filter + evidence scorer (telemetry-only slots, written after gate).
    state.slots["last_spoken_reply"] = reply_text or ""
    state.slots["_last_borrower_transcript"] = request.transcript or ""

    with StageTimer(latency, "persist"):
        audit_id = await _persist_turn(memory, state, borrower, request, audit_chain)

    annotate_turn_span(turn_span, chain=audit_chain, latency=latency, llm_calls=llm_calls)
    # DEBT-039: speak the third-party close + callback before end_call (strict)
    # or before continuing (relaxed). The close reply interpolates
    # {customer_name} and contains zero loan facts (identity is revoked).
    await _emit_preempt_close(on_gated_reply, reply_text, state, request)
    return TurnResponse(
        reply_text=reply_text,
        end_call=(flip_mode == "strict"),
        transfer_to_human=False,
        actions_executed=[],
        disposition="THIRD_PARTY_FLAGGED",
        state_version=state.version,
        audit_id=audit_id,
    )


async def _run_closed_early_exit(
    request: TurnRequest,
    state: ConversationState,
    borrower: BorrowerRecord,
    memory: Any,
    latency: TurnLatencyProfile,
    turn_span: Any,
    llm_calls: int,
    *,
    brand_pack: BrandOverridePack | None = None,
) -> TurnResponse:
    """Terminal turn: the call was already closed (hangup/transfer) on a prior turn.

    Once a flow has hung up or handed off, a late barge-in ("ok, bye") must NOT restart
    the script — otherwise the call sits on a generic clarify with an empty flow stack
    and never disconnects. We skip command-gen/executor entirely and just re-issue
    end_call so the carrier tears the leg down. No line is spoken (the closing/handoff
    line already played on the turn that set the close).

    Exception: while a WARM TRANSFER is pending (agent still ringing), end_call is
    suppressed — ending the bot leg would tear the whole Stasis-owned call down before
    the agent joins. Teardown is orchestrator-driven in every transfer outcome
    (complete drops the AI leg; failure hangs up the customer leg), so the call can
    never idle forever.
    """
    transfer_pending = str(state.slots.get("transfer_status") or "") == "pending"
    state = apply(
        state,
        [
            Event(
                ts=datetime.now(UTC).isoformat(),
                kind="call_closed",
                data={"transcript_len": len(request.transcript)},
            ),
        ],
    )
    state.attempts += 1
    _, state, transfer, audit_chain = process_outbound_reply(
        "",
        state,
        request,
        safety_reason=None,
        latency=latency,
        llm_calls=llm_calls,
        manifest_version=MANIFEST_VERSION,
        brand_pack=brand_pack,
    )
    log_turn_decision(
        session_id=request.call_id,
        transcript=request.transcript,
        borrower=borrower,
        kb_candidates=[],
        commands=[],
        rejected_slots=[],
        state=state,
        reply_id=None,
        gate_verdict=audit_chain.gate_verdict,
        gate_reason="call_closed",
        draft_reply="",
        final_reply="",
    )
    with StageTimer(latency, "persist"):
        audit_id = await _persist_turn(memory, state, borrower, request, audit_chain)
    annotate_turn_span(turn_span, chain=audit_chain, latency=latency, llm_calls=llm_calls)
    return TurnResponse(
        reply_text="",
        end_call=not transfer_pending,
        transfer_to_human=transfer,
        actions_executed=[],
        disposition=(
            str(state.slots["disposition"])
            if state.slots.get("disposition") is not None
            else None
        ),
        state_version=state.version,
        audit_id=audit_id,
    )


async def _run_echo_hold_early_exit(
    request: TurnRequest,
    state: ConversationState,
    borrower: BorrowerRecord,
    memory: Any,
    latency: TurnLatencyProfile,
    turn_span: Any,
    llm_calls: int,
    *,
    brand_pack: BrandOverridePack | None = None,
) -> TurnResponse:
    """W2-1 echo filter HOLD: the transcript is a near-repeat of the bot's last
    spoken reply (speaker echo leaked back into ASR). Drop the turn with ZERO
    counter burn — no attempts++, no LLM call, no flow advance, no repair-counter
    tick, no reply spoken. ``last_spoken_reply`` and ``_last_borrower_transcript``
    are NOT overwritten (the prior bot line stays the "last spoken" so the next
    real turn can still echo-match against it; the echo itself is not a real
    borrower utterance). Outcome=HOLD, ``echo_suspected=true``, ``evidence=0``.
    """
    state = apply(
        state,
        [
            Event(
                ts=datetime.now(UTC).isoformat(),
                kind="echo_hold",
                data={
                    "transcript_len": len(request.transcript),
                    "last_spoken_reply_len": len(state.slots.get("last_spoken_reply") or ""),
                },
            ),
        ],
    )
    # Empty gated reply so the audit chain + gate verdict are recorded, but
    # nothing is spoken (the bot stays silent on an echo turn).
    reply_text, state, _transfer, audit_chain = process_outbound_reply(
        "",
        state,
        request,
        safety_reason="echo_hold",
        latency=latency,
        llm_calls=llm_calls,
        manifest_version=MANIFEST_VERSION,
        brand_pack=brand_pack,
    )
    log_turn_decision(
        session_id=request.call_id,
        transcript=request.transcript,
        borrower=borrower,
        kb_candidates=[],
        commands=[],
        rejected_slots=[],
        state=state,
        reply_id=None,
        gate_verdict=audit_chain.gate_verdict,
        gate_reason="echo_hold",
        draft_reply="",
        final_reply="",
        guards={
            "echo_suspected": True,
            "evidence": 0,
            "evidence_reason": "echo",
            "outcome": "HOLD",
            "final_text_len": 0,
        },
    )
    with StageTimer(latency, "persist"):
        audit_id = await _persist_turn(memory, state, borrower, request, audit_chain)
    annotate_turn_span(turn_span, chain=audit_chain, latency=latency, llm_calls=llm_calls)
    return TurnResponse(
        reply_text="",
        end_call=False,
        transfer_to_human=False,
        actions_executed=[],
        disposition="ECHO_HOLD",
        state_version=state.version,
        audit_id=audit_id,
    )


async def handle_turn(
    request: TurnRequest,
    *,
    memory: Any,
    kb: Any,
    llm: Any,
    tools: Any,
    flows: FlowSet | None = None,
    overrides: OverrideProvider | None = None,
    on_gated_reply: Any | None = None,
) -> TurnResponse:
    """Full turn loop: safety → retrieval → command_gen → executor → nlg → gate → persist."""
    latency = TurnLatencyProfile()
    llm_calls = 0
    if flows is None:
        flows = get_flow_set()
    override_provider = overrides or NullOverrideProvider()
    tenant_cfg = tenant_config(request.tenant_id)
    brand_pack: BrandOverridePack | None = None
    pack_rejected = False
    pack_rejected_reason: str | None = None

    with turn_trace(request.call_id, request.borrower_id, request.tenant_id) as turn_span:
        with StageTimer(latency, "load_state"):
            state = await memory.load_state(request.call_id)
            if state is None:
                state = new_conversation_state(
                    request.call_id,
                    request.tenant_id,
                    request.borrower_id,
                )
            # R2-DB: borrower_id=="unknown"/"" is a sentinel — never load_borrower
            # by it (a malicious/stale row with id="unknown" must not hydrate).
            # The phone lookup below resolves the real borrower; if none, the
            # unknown-borrower path (elif borrower is None) constructs a placeholder.
            if request.borrower_id and request.borrower_id not in {"", "unknown"}:
                borrower = await memory.load_borrower(request.borrower_id)
            else:
                borrower = None
            settings = get_settings()
            sot_override = (settings.test_sot_scenario or "").strip().lower()
            plo_override = (settings.test_plo_scenario or "").strip().lower()
            # DEBT-018: quarantine the test-mode borrower selection behind profile
            # fields. ``allow_sot_test_mode`` distinguishes the SOT fixture path
            # (uses sot_override) from the PLO path (uses plo_override);
            # ``test_borrower_factory`` names the hardcoded_*_borrower callable.
            # Open tenants (no profile) skip fixtures entirely.
            _hydrate_profile = get_tenant_profile(request.tenant_id or "")
            sot_test_mode = (
                settings.test_mode
                and request.tenant_id == settings.test_tenant_id
                and _hydrate_profile is not None
                and _hydrate_profile.allow_sot_test_mode
            )
            plo_test_mode = (
                settings.test_mode
                and _hydrate_profile is not None
                and not _hydrate_profile.allow_sot_test_mode
                and bool(_hydrate_profile.test_borrower_factory)
            )
            # Fixtures fire ONLY when the scenario env var is explicitly set; they
            # then force a scenario regardless of DB (that's their job). When unset,
            # the DB borrower wins; if DB has nothing, proceed as unknown borrower
            # (no silent fixture fallback).
            if sot_test_mode and sot_override:
                _factory = _resolve_test_borrower_factory(
                    _hydrate_profile.test_borrower_factory
                )
                borrower = _factory(
                    request.borrower_id or _hydrate_profile.test_borrower_id
                )
            elif plo_test_mode and plo_override:
                _factory = _resolve_test_borrower_factory(
                    _hydrate_profile.test_borrower_factory
                )
                borrower = _factory(
                    request.borrower_id or _hydrate_profile.test_borrower_id
                )
            elif borrower is None:
                borrower = BorrowerRecord(borrower_id=request.borrower_id)
            state = hydrate_from_borrower(state, borrower)
            if settings.test_mode:
                from app.memory.test_borrower import apply_test_borrower_slots

                state = apply_test_borrower_slots(state, borrower)
            state = hydrate_followup_from_borrower(state, borrower)
            state = apply_trust_to_state(state, borrower)
            state = apply_risk_to_state(state, borrower)
            state = apply_persona_to_state(state, borrower)
            state = apply_recovery_to_state(state, borrower)
            if request.turn_meta.get("call_date"):
                state.slots["call_date"] = request.turn_meta["call_date"]
            if request.turn_meta.get("force_flow"):
                state.slots["_force_test_flow"] = str(request.turn_meta["force_flow"])
            borrower_ctx = normalize_borrower_context(request.turn_meta.get("borrower_context"))
            lookup_by_phone = getattr(memory, "lookup_borrower_by_phone", None)
            phone = borrower_ctx.get("phone") if borrower_ctx else None
            if phone and callable(lookup_by_phone) and not sot_test_mode:
                if request.borrower_id in {"", "unknown"} or not borrower.identity.get("name"):
                    db_borrower = await lookup_by_phone(phone, tenant_id=request.tenant_id)
                    # R2-DB: ignore sentinel/stale rows (borrower_id in {"","unknown"})
                    # so a malicious id="unknown" row can't be hydrated over the real
                    # seeded borrower; fall through to unknown-borrower path instead.
                    if db_borrower is not None and db_borrower.borrower_id not in {"", "unknown"}:
                        borrower = apply_borrower_context_to_record(db_borrower, borrower_ctx or {})
                        state.borrower_id = db_borrower.borrower_id
                        # Re-hydrate loan slots from the DB borrower so select_plo_scenario
                        # (dpd/npa_flag/product) and the Tier-3 respond/grounding path
                        # (branch/branch_address) read the seeded row, not the placeholder.
                        state = hydrate_from_borrower(state, borrower)
                        state = hydrate_followup_from_borrower(state, borrower)
            if borrower_ctx:
                state = apply_borrower_context_to_state(state, borrower_ctx)
                borrower = apply_borrower_context_to_record(borrower, borrower_ctx)

            emotion = classify_emotion_from_turn(
                request.transcript,
                turn_meta=request.turn_meta,
                channel=request.channel,
            )
            state = apply_emotion_to_state(state, emotion)
            _profile_early = get_tenant_profile(request.tenant_id)
            state, frustration_escalate = track_frustration(
                state,
                emotion=emotion.emotion,
                intensity=emotion.intensity,
                threshold=(
                    _profile_early.frustration_escalate_turns
                    if _profile_early is not None
                    else 0
                ),
            )

            state = apply_identity_entry_gate(state, flows)

            forced_flow = state.slots.get("_force_test_flow")
            # Allow any loaded flow as a force target (aliases kept for agent routing;
            # profile tenants may force their opener without editing FORCE_FLOW_ALIASES).
            if isinstance(forced_flow, str) and (
                forced_flow in FORCE_FLOW_ALIASES or forced_flow in flows.flows
            ):
                # DEBT-017 guard: a forced flow must belong to the active tenant's
                # catalog (prevents a plo_ call from injecting a sot_ flow via
                # force_flow — NLG Leak Path A). Profile tenants whose catalog does
                # not contain the forced flow drop the injection. Open tenants
                # (no profile) keep the legacy behaviour.
                _ff_profile = get_tenant_profile(request.tenant_id or "")
                if (
                    _ff_profile is not None
                    and forced_flow in flows.flows
                    and forced_flow
                    not in {
                        entry["name"]
                        for entry in tenant_flow_catalog(_ff_profile, flows)
                    }
                ):
                    forced_flow = None
                if isinstance(forced_flow, str) and forced_flow:
                    stack_names = {frame.flow for frame in state.flow_stack}
                    already_injected = (
                        state.slots.get("_forced_flow_injected") == forced_flow
                    )
                    if (
                        forced_flow not in stack_names
                        and forced_flow in flows.flows
                        and not already_injected
                    ):
                        state.flow_stack.append(Frame(flow=forced_flow, step_index=0))
                        state.slots["_forced_flow_injected"] = forced_flow

            brand_pack = await _stash_brand_pack(state, override_provider, request)

        # W2-1 echo filter: runs BEFORE policy preempts so the bot's own spoken
        # legal lines (DNC ack, vulnerability close, third-party script, opener
        # greeting) cannot self-trigger the policy lane when speaker echo leaks
        # back into the mic. On echo match: drop the turn — outcome=HOLD,
        # echo_suspected=true, evidence=0, ZERO counter burn (no attempts++, no
        # LLM call, no flow advance, no repair-counter tick). Precedence per
        # W2_SPRINT_SPEC.md invariant #2: echo → preempts → scorer → router.
        with StageTimer(latency, "echo_filter"):
            _last_spoken = (state.slots.get("last_spoken_reply") or "")
            _echo_hit = detect_echo(request.transcript, _last_spoken)
        if _echo_hit:
            return await _run_echo_hold_early_exit(
                request,
                state,
                borrower,
                memory,
                latency,
                turn_span,
                llm_calls,
                brand_pack=brand_pack,
            )

        # W1-C policy preempts run BEFORE the terminal guard so the disposition is
        # truthful even on superseded / terminal-race turns: a third-party / DNC /
        # vulnerability cue in the final merged transcript classifies the turn
        # (THIRD_PARTY_FLAGGED / dnc_requested / VULNERABLE_FLAGGED) even when a
        # prior turn already set end_call (DEBT-030). Precedence: safety -> dnc ->
        # call_window -> third_party -> terminal guard fallback.
        with StageTimer(latency, "safety_preempt"):
            state, safety_reply = safety_check_transcript(request, state)
        if safety_reply is not None:
            return await _run_safety_early_exit(
                request,
                state,
                borrower,
                memory,
                latency,
                turn_span,
                llm_calls,
                safety_reply,
                brand_pack=brand_pack,
                on_gated_reply=on_gated_reply,
            )

        # W1-C C2 (DNC/opt-out capture, policy interrupt): runs BEFORE the
        # Tier-1 evidence scorer, always preempts, lands in outcome 7 (graceful
        # END) with disposition=dnc_requested. Non-committal ack — does NOT
        # promise dialer suppression until W4.
        with StageTimer(latency, "dnc_preempt"):
            state, dnc_reply, is_dnc = dnc_check_transcript(request, state)
        if is_dnc and dnc_reply is not None:
            return await _run_dnc_early_exit(
                request,
                state,
                borrower,
                memory,
                latency,
                turn_span,
                llm_calls,
                dnc_reply,
                brand_pack=brand_pack,
                on_gated_reply=on_gated_reply,
            )

        # W1-C C3 (call-window close-out, policy interrupt): an answered call
        # crossing the configured window boundary mid-conversation gets a
        # scripted polite close + hangup — never a mid-call silent_reply.
        # Runs BEFORE the Tier-1 evidence scorer; lands in outcome 7 with
        # disposition=call_window_closed.
        with StageTimer(latency, "call_window_preempt"):
            state, cw_reply, is_cw_close = call_window_check_transcript(request, state)
        if is_cw_close and cw_reply is not None:
            return await _run_call_window_early_exit(
                request,
                state,
                borrower,
                memory,
                latency,
                turn_span,
                llm_calls,
                cw_reply,
                brand_pack=brand_pack,
                on_gated_reply=on_gated_reply,
            )

        # W1-C C4 (third-party / speaker-flip guard, policy interrupt): a
        # different speaker joins or takes over the call mid-conversation →
        # identity_current revoked → disclosure LOCK (strict) or generic-only
        # (relaxed) → third-party script + callback capture → END (strict) or
        # continue (relaxed). DPDP posture is brand-configurable (amendment).
        # ALWAYS-ON: third_party_suspected + identity_current transition logged.
        with StageTimer(latency, "third_party_flip_preempt"):
            state, flip_reply, is_flip, flip_mode = third_party_flip_check_transcript(request, state)
        if is_flip:
            return await _run_third_party_flip_early_exit(
                request,
                state,
                borrower,
                memory,
                latency,
                turn_span,
                llm_calls,
                flip_reply,
                flip_mode,
                brand_pack=brand_pack,
                on_gated_reply=on_gated_reply,
            )

        # Terminal guard (fallback): if a prior turn already closed the call
        # (hangup_call / transfer_call set end_call + call_closed) AND no policy
        # preempt fired on this turn's transcript, re-issue end_call so the call
        # disconnects instead of idling on a generic clarify with an empty flow
        # stack. Preempts run first so a third-party / DNC / vulnerability cue in
        # the final transcript still classifies truthfully (DEBT-030).
        _term_profile = get_tenant_profile(request.tenant_id)
        _closed_slot = (
            (_term_profile.call_closed_slot if _term_profile else None)
            or "sot_call_closed"
        )
        if state.slots.get(_closed_slot) or state.slots.get("end_call"):
            return await _run_closed_early_exit(
                request,
                state,
                borrower,
                memory,
                latency,
                turn_span,
                llm_calls,
                brand_pack=brand_pack,
            )

        state.attempts += 1

        candidate_flows: list[dict[str, Any]] = []
        commands: list[Command] = []
        command_rejections: list[str] = []
        dispute_forced: str | None = None
        exec_result = ExecResult(state=state)
        turn_event = Event(
            ts=datetime.now(UTC).isoformat(),
            kind="turn",
            data={
                "tenant_id": request.tenant_id,
                "transcript_len": len(request.transcript),
                "channel": request.channel,
            },
        )

        # Scripted-tenant on-rails status. Closed calls suppress new starts; blank
        # transcript gets an empty candidate list. Profiled tenants use Tier-2 catalog
        # routing by default (SCRIPTED_CATALOG_ROUTING); open/default tenants keep RAG.
        profile = get_tenant_profile(request.tenant_id)
        sot_awaiting_slot = ""
        sot_on_rails = False
        sot_closed = False
        sot_blank_transcript = False
        if profile is not None:
            state = _sc.prune_spurious_objection_stack(state, profile, flows)
            sot_awaiting_slot = _awaiting_collect_slot(state, flows)
            active_flow = state.flow_stack[-1].flow if state.flow_stack else ""
            sot_on_rails = (
                active_flow in profile.onrails_flows
                or sot_awaiting_slot in profile.commit_collect_slots
            )
            closed_slot = profile.call_closed_slot or f"{profile.flow_prefix}call_closed"
            sot_closed = bool(
                state.slots.get(closed_slot) or state.slots.get("end_call")
            )
            sot_blank_transcript = _sc.transcript_blank(request.transcript)

        catalog_mode = bool(
            profile is not None
            and bool(getattr(settings, "scripted_catalog_routing", True))
        )
        # Legacy digression path (catalog off): on-rails skip KB unless digression on.
        sot_digression = (
            profile is not None
            and not catalog_mode
            and bool(getattr(settings, "sot_digression_enabled", False))
        )
        skip_retrieval = profile is not None and (
            catalog_mode
            or sot_closed
            or (sot_on_rails and not sot_digression)
            or sot_blank_transcript
        )

        sot_blocked_commands: frozenset[str] = frozenset()
        full_catalog_names: frozenset[str] = frozenset()
        if catalog_mode:
            # Tier 2: full tenant catalog; never call KB retrieval.
            with span("retrieval", external=True):
                with StageTimer(latency, "retrieval", external=True):
                    pass
            sot_blocked_commands = profile.blocked_commands  # type: ignore[union-attr]
            if sot_closed or sot_blank_transcript:
                candidate_flows = []
            else:
                full_catalog = tenant_flow_catalog(profile, flows)  # type: ignore[arg-type]
                full_catalog_names = frozenset(
                    str(c.get("name") or "") for c in full_catalog if c.get("name")
                )
                # W2-4b D3: offer the state-scoped catalog (scenario + slot
                # tags + universals). Untagged tenants fall back to full.
                candidate_flows = build_scoped_catalog(
                    profile, flows, state, sot_awaiting_slot  # type: ignore[arg-type]
                )
                # While awaiting a commit/push collect slot, drop deflection objections
                # (busy/hold/…). Disputes + info objections stay in the catalog.
                if sot_awaiting_slot in profile.commit_collect_slots:  # type: ignore[union-attr]
                    candidate_flows = filter_deflection_objections(
                        candidate_flows, profile  # type: ignore[arg-type]
                    )
        else:
            # Non-profile tenants (and catalog-off rollback): RAG path unchanged.
            candidates = []
            with span("retrieval", external=True):
                with StageTimer(latency, "retrieval", external=True):
                    if not skip_retrieval:
                        candidates = await retrieve_flow_candidates(
                            kb,
                            request.transcript,
                            request.tenant_id,
                        )
            candidate_flows = [
                {"name": c.name, "description": c.description, "score": c.score}
                for c in candidates
            ]
            if profile is not None:
                prefix = profile.flow_prefix
                obj_prefix = profile.objection_prefix
                candidate_flows = [
                    c
                    for c in candidate_flows
                    if str(c.get("name", "")).startswith(prefix)
                ]
                sot_blocked_commands = profile.blocked_commands
                if sot_closed:
                    candidate_flows = []
                elif sot_on_rails and not sot_digression:
                    candidate_flows = [
                        c
                        for c in candidate_flows
                        if not str(c.get("name", "")).startswith(obj_prefix)
                    ]
                elif sot_digression:
                    candidate_flows = _merge_pinned_flow_candidates(
                        candidate_flows, list(profile.pinned_flows), flows
                    )

        respond_enabled = bool(profile.respond_enabled) if profile is not None else False
        unknown_info_reply = (
            (profile.unknown_info_reply or "") if profile is not None else ""
        )

        # Item 2 (DEBT-034): skip the LLM for the opener blank turn on scripted
        # tenants. The flow walker renders the deterministic greeting from the
        # forced opener flow (already on flow_stack via the force_flow injection
        # above); the LLM's `respond` is always rejected for a blank transcript,
        # so the 843ms LLM round-trip is wasted. Preempts (safety/dnc/call_window/
        # third_party) already ran above; this only short-circuits the LLM call.
        # Guard: scripted tenant (profile) + forced opener flow + blank transcript.
        _forced = str(state.slots.get("_force_test_flow") or "")
        _opener_skip_llm = (
            profile is not None
            and _forced.endswith("_opener")
            and not (request.transcript or "").strip()
        )
        # W2-4b D1: cue-pack already routes the turn (willing / identity /
        # callback / refusal / dispute) — skip command_gen like the opener
        # skip. Question-shaped transcripts never skip (E3).
        cue_hit_skip = False
        class_cache_hit = False
        from app.engine.robustness import PENDING_CONFIRM_KEY
        _early_pending = state.slots.get(PENDING_CONFIRM_KEY)
        cue_pack = (
            _sc.cue_hit_pack(
                request.transcript,
                sot_awaiting_slot,
                profile=profile,
                on_rails=sot_on_rails,
                borrower_name=str(
                    state.slots.get("customer_name")
                    or state.slots.get("borrower_name")
                    or ""
                ),
                pending_confirm=_early_pending if isinstance(_early_pending, dict) else None,
            )
            if profile is not None and not _opener_skip_llm
            else None
        )
        _class_key = (
            f"{sot_awaiting_slot}|{' '.join((request.transcript or '').lower().split())}"
        )
        _class_cache = state.slots.get("_cmd_class_cache")
        if not isinstance(_class_cache, dict):
            _class_cache = {}

        if _opener_skip_llm:
            parse_result = CommandParseResult(commands=[], rejections=[], raw="")
            commands = []
            command_rejections = []
            llm_calls = 0
        elif cue_pack:
            parse_result = CommandParseResult(commands=[], rejections=[], raw="")
            commands = []
            command_rejections = []
            llm_calls = 0
            cue_hit_skip = True
        elif _class_key in _class_cache and isinstance(_class_cache[_class_key], str):
            # W2-4b D2: in-session classification cache (repeat transcript).
            parse_result = parse_and_validate_commands(
                _class_cache[_class_key],
                candidate_flows=candidate_flows,
                blocked_commands=sot_blocked_commands,
                catalog_mode=catalog_mode,
                respond_enabled=respond_enabled,
                full_catalog_names=full_catalog_names,
            )
            commands = parse_result.commands
            command_rejections = parse_result.rejections
            llm_calls = 0
            class_cache_hit = True
        else:
            with span("command_gen", external=True):
                with StageTimer(latency, "command_gen", external=True):
                    parse_result = await generate(
                        request.transcript,
                        state,
                        candidate_flows,
                        llm=llm,
                        blocked_commands=sot_blocked_commands,
                        catalog_mode=catalog_mode,
                        respond_enabled=respond_enabled,
                        unknown_info_reply=unknown_info_reply,
                        full_catalog_names=full_catalog_names,
                    )
                    commands = parse_result.commands
                    command_rejections = parse_result.rejections
                    llm_calls = 1

        coercion_meta: dict[str, str | None] = {"refusal_matched_via": None}
        _ptp_partial: dict | None = None
        _ptp_result: dict | None = None
        _today = _sc.today_ist(state.slots.get("call_date") or state.slots.get("today"))
        if profile is not None:
            commands, coercion_meta = _sc.run_coercion_chain(
                commands,
                sot_awaiting_slot,
                request.transcript,
                profile=profile,
                on_rails=sot_on_rails,
                blank_transcript=sot_blank_transcript,
                pending_confirm=_early_pending if isinstance(_early_pending, dict) else None,
                today=_today,
            )
            _date_ask = coercion_meta.get("date_ask")
            if _date_ask:
                _ask_fid = (
                    "ask_pay_date_nearer" if _date_ask == "nearer" else "ask_pay_date"
                )
                commands = [
                    c
                    for c in commands
                    if not (
                        c.command == "set_slot"
                        and c.name in {
                            sot_awaiting_slot,
                            "plo_payment_intent",
                            "plo_timeline",
                            "committed_date",
                        }
                    )
                ]
                commands.append(
                    Command(
                        command="compose",
                        fragments=[_ask_fid],
                        oof_class="payment_assertion",
                    )
                )
            elif not _date_ask:
                from app.engine.ptp_policy import partial_pre_gate, policy_from_profile

                _ptp_cfg = policy_from_profile(profile)
                if _ptp_cfg is not None:
                    _partial = partial_pre_gate(
                        commands=commands,
                        transcript=request.transcript,
                        slots=dict(state.slots),
                        policy=_ptp_cfg,
                    )
                    if _partial is not None:
                        commands = _partial["commands"]
                        _ptp_partial = _partial

        # Declarative slot validation (F4, tenant-agnostic): drop set_slots that would
        # overwrite hydrated facts or fill a typed slot with the wrong kind of answer,
        # so the executor cleanly re-asks (bounded by F1/F2) instead of advancing on
        # garbage.
        commands, dropped_slots = validate_commands(commands)
        if dropped_slots:
            command_rejections = [*command_rejections, *dropped_slots]

        # Clarification on ambiguous flow candidates (F6). Gated per tenant; off for
        # salary_on_time (candidates already constrained), on for open tenants.
        # Catalog mode has no scores — _clarify_if_ambiguous no-ops.
        if tenant_cfg.clarify_on_ambiguous_flow:
            commands, ambiguous = _clarify_if_ambiguous(
                commands, candidate_flows, delta=tenant_cfg.flow_ambiguity_delta
            )
            if ambiguous:
                command_rejections = [
                    *command_rejections,
                    "clarified ambiguous flow candidates",
                ]

        # Layer 3 (legacy digression only): suppress weak KB-scored jumps mid-collect.
        # Dead under catalog mode (no scores / no digression).
        weak_jump_suppressed = False
        if sot_digression and sot_awaiting_slot:
            commands, weak_jump_suppressed = _suppress_low_confidence_flow_jumps(
                commands,
                candidate_flows,
                pinned_names=frozenset(profile.pinned_flows if profile else ()),
                floor=float(settings.sot_flow_confidence_floor),
            )
            if weak_jump_suppressed:
                command_rejections = [
                    *command_rejections,
                    "suppressed low-confidence flow jump",
                ]

        # Cross-turn evidence accumulator: deterministic matcher OR LLM-proposed
        # dispute start_flow. Never mere candidate presence.
        if profile is not None:
            evidence_theme = _dispute_evidence_this_turn(
                request.transcript,
                parse_result.commands,
                frozenset(profile.dispute_flows),
                profile=profile,
            )
            state, commands, dispute_forced = _accumulate_dispute_evidence(
                state,
                commands,
                evidence_theme,
                bar=int(settings.sot_dispute_evidence_bar),
            )
            if dispute_forced:
                command_rejections = [
                    *command_rejections,
                    f"forced dispute route via accumulator: {dispute_forced}",
                ]

        # Label Transition Layer (LTL). Runs after all command shaping and before
        # tracker.apply. Behind LABEL_TRANSITION_ENABLED (default off). In shadow mode it
        # only observes/records labels; in enforce mode (supported providers only, e.g.
        # salary_on_time) it may rewrite command primitives. Never mutates flow_stack.
        label_decision = None
        try:
            state, commands, label_decision = run_label_transition(
                state=state,
                commands=commands,
                transcript=request.transcript,
                awaiting_slot=sot_awaiting_slot,
                candidate_flows=candidate_flows,
                tenant_id=request.tenant_id,
                flows=flows,
                settings=settings,
                dispute_forced=dispute_forced,
            )
            if label_decision is not None and label_decision.enforcement_applied:
                command_rejections = [
                    *command_rejections,
                    f"label transition enforced: {label_decision.decision}",
                ]
        except Exception:  # noqa: BLE001 — LTL must never break a live turn
            logger.exception("label_transition failed; continuing without it")
            label_decision = None

        # Belt: blank transcript must never speak respond (greeting/template wins).
        # Sanitize may already have stripped respond; still log the rejection when
        # the LLM emitted one (parse_result) so dumps show the belt fired.
        if sot_blank_transcript:
            llm_had_respond = any(c.command == "respond" for c in parse_result.commands)
            if any(c.command == "respond" for c in commands):
                commands = [c for c in commands if c.command != "respond"]
                llm_had_respond = True
            if llm_had_respond:
                command_rejections = [
                    *command_rejections,
                    "respond rejected: blank transcript",
                ]

        # Tier-3 respond / W2-3 compose: hold text aside (no flow frame); keep
        # in audit payload. compose is the new W2-3 lane (<=2 fragment ids +
        # oof_class); respond is the legacy Tier-3 free-text escape hatch
        # (demoted per invariant #4 — fires only when compose returns no
        # viable fragments; escape_hatch_used logged).
        respond_fired = False
        respond_text_raw = ""
        compose_fired = False
        compose_fragment_ids: list[str] = []
        compose_rejections: list[str] = []
        compose_reply_text = ""
        for cmd in commands:
            if cmd.command == "compose" and cmd.fragments:
                compose_fired = True
                compose_fragment_ids = list(cmd.fragments)
                break
            if cmd.command == "respond" and (cmd.text or "").strip():
                respond_fired = True
                respond_text_raw = (cmd.text or "").strip()
                break

        # W2-3 UNRELATED deterministic lane (invariant #8). oof_class=irrelevant
        # → ALWAYS render a scope-boundary fragment (pre/post-identity variant)
        # + canonical re-ask. World-knowledge / RAG / tools / Tier-3 OFF. The
        # "answer" for unrelated never means content. Deterministic — no LLM
        # content is rendered for irrelevant turns.
        unrelated_redirect = False
        if parse_result.oof_class == "irrelevant":
            unrelated_redirect = True
            compose_fired = True
            compose_fragment_ids = []  # renderer picks scope_boundary variant
            respond_fired = False  # irrelevant suppresses Tier-3 respond

        # W2-3 compose validation + rendering. Validate the selection (ids
        # exist, ack pair-only, scenario/product gates, unhydrated slot →
        # unknown_info) then render → reply text. For the UNRELATED lane the
        # renderer picks the scope_boundary variant by identity_ok.
        _compose_slots = dict(state.slots)
        try:
            from app.engine.ptp_policy import compute_derived_slots

            _compose_slots.update(compute_derived_slots(_compose_slots, _today))
        except Exception:
            pass
        if _ptp_partial:
            _compose_slots.update(_ptp_partial.get("render_overlay") or {})
        if compose_fired:
            if unrelated_redirect:
                compose_reply_text = render_unrelated_redirect(
                    request.tenant_id,
                    identity_ok=bool(state.slots.get("identity_ok")),
                    state_slots=_compose_slots,
                    persona_voice=getattr(profile, "voice_id", None) if profile else None,
                )
                compose_fragment_ids = (
                    ["scope_boundary_post_identity"]
                    if state.slots.get("identity_ok")
                    else ["scope_boundary_pre_identity"]
                )
            else:
                compose_fragment_ids, compose_rejections = validate_compose(
                    request.tenant_id,
                    compose_fragment_ids,
                    scenario=getattr(profile, "scenario", None) if profile else None,
                    product=getattr(profile, "product", None) if profile else None,
                    state_slots=_compose_slots,
                )
                compose_reply_text = render_compose(
                    request.tenant_id,
                    compose_fragment_ids,
                    _compose_slots,
                    persona_voice=getattr(profile, "voice_id", None) if profile else None,
                )
            # compose replaces respond — the rendered fragment text IS the
            # reply text. Suppress the legacy respond escape hatch.
            if compose_reply_text:
                respond_fired = False
                respond_text_raw = ""

        commands_payload = [cmd.model_dump(mode="json") for cmd in commands]
        apply_commands = (
            [c for c in commands if c.command not in ("respond", "compose")]
            if (respond_fired or compose_fired)
            else commands
        )

        # W2-3 escape-hatch telemetry: Tier-3 respond is the escape hatch
        # (invariant #4). escape_hatch_used=true when respond fires (compose
        # missed). Target metric <5% of OOF turns.
        escape_hatch_used = respond_fired and not compose_fired

        # W2-2 Commitment Gate (SHADOW this phase). The gate is a pure function
        # over (candidate_commands, evidence, cost_table, identity_ok) →
        # {execute | downgrade_to_confirm | hold}. It sits AFTER propose
        # (command_gen → coercion → validation → clarify → dispute evidence
        # → LTL → blank belt → respond hold-aside) and BEFORE commit
        # (tracker_apply → priority_reorder → decision_overlay → executor).
        # In SHADOW (COMMITMENT_GATE_ENFORCE=false, default) the gate only
        # LOGS its verdict; the commit path runs unchanged. In ENFORCE the
        # gate will block tracker_apply and replace apply_commands with a
        # confirm-ask fragment (W2-2 follow-up after the shadow observation
        # week). The gate consumes ONLY the deterministic evidence score
        # (W2-1) — never LLM confidence (invariant #6).
        #
        # Grep-proof: no slot write, PTP record, flow advance, or end_call
        # occurs before this line. The propose stages above (command_gen,
        # coercion, validation, clarify, dispute evidence, LTL) build the
        # candidate; the commit stages below (tracker_apply, executor) apply
        # it. The gate is the seam.
        # W2-4: capture the prior turn's _pending_confirm BEFORE the gate
        # runs. score_evidence uses it to score yes-tokens as evidence 3
        # (explicit_confirm) when the gate issued a confirm-ask last turn;
        # track_slot_reask_gated uses it to detect failed confirms. The
        # gate manages _pending_confirm going forward (sets on downgrade,
        # clears on execute/hold) — we must read the prior value first.
        from app.engine.robustness import PENDING_CONFIRM_KEY
        _prior_pending_confirm = state.slots.get(PENDING_CONFIRM_KEY)
        _question_shape = has_question_shape(request.transcript)
        _pending_value = None
        _pending_date = None
        if isinstance(_prior_pending_confirm, dict):
            _pending_value = _prior_pending_confirm.get("value")
            _pending_date = _prior_pending_confirm.get("committed_date")
        _evidence = score_evidence(
            transcript=request.transcript,
            state=state,
            profile=profile,
            llm_calls=llm_calls,
            commands=commands,
            last_spoken_reply=state.slots.get("last_spoken_reply") or "",
            echo=False,
            awaited_slot=sot_awaiting_slot or None,
            pending_confirm=bool(_prior_pending_confirm),
            pending_value=_pending_value,
            pending_date=str(_pending_date) if _pending_date else None,
            today=_today,
        )
        # P4: locked-refuse re-refusal → evidence 3 so the gate executes
        # attempt-2 directly (no second confirm).
        _locked = state.slots.get(LOCKED_SLOT_VALUES_KEY)
        if isinstance(_locked, dict):
            _refused = {"refused", "refuse", "unwilling", "later", "denied", "no"}
            for _c in commands:
                if _c.command != "set_slot" or not _c.name:
                    continue
                _lv = str(_locked.get(_c.name) or "").strip().lower()
                _cv = str(_c.value or "").strip().lower()
                if _lv in _refused and _cv in _refused:
                    _evidence = {
                        **_evidence,
                        "evidence": 3,
                        "evidence_reason": "locked_refuse_restatement",
                    }
                    break
        # E3: question-shape (हाँ + "ऑफिस कहाँ है?") is answer-first.
        # Strip money-state writes so a leading yes-token cannot commit
        # willing / date / amount. The question (respond / start_flow)
        # still goes through the gate.
        _e3_stripped_money: list[str] = []
        if _question_shape:
            _slot_cc = (
                profile.commitment_gate_slot_cost_class if profile else {}
            ) or {}
            kept: list[Command] = []
            for c in apply_commands:
                if c.command == "set_slot" and c.name:
                    if _slot_cost_class(c.name, _slot_cc) == "money_state":
                        _e3_stripped_money.append(c.name)
                        continue
                kept.append(c)
            apply_commands = kept
        _flow_gate_class = flow_gate_class_map(flows)
        _gate_verdict = commitment_gate(
            apply_commands,
            evidence=_evidence,
            cost_table=(profile.commitment_gate_cost_table if profile else None),
            slot_cost_class=(profile.commitment_gate_slot_cost_class if profile else None),
            identity_ok=bool(state.slots.get("identity_ok")),
            awaited_slot=sot_awaiting_slot or None,
            flow_gate_class=_flow_gate_class,
        )
        # SHADOW: log only, do not alter apply_commands. ENFORCE: block the
        # commit path — downgrade → replace apply_commands with a confirm-ask
        # fragment (confirm_fragment_id) + set _pending_confirm; hold → drop
        # apply_commands (re-ask only). The gate consumes ONLY the
        # deterministic evidence score (W2-1) — never LLM confidence.
        _gate_enforce = commitment_gate_enforce_enabled()
        _gate_blocked_writes: list[str] = list(_e3_stripped_money)
        if _gate_enforce:
            if _gate_verdict["verdict"] == "downgrade":
                # Block the candidate writes; replace with a confirm-ask
                # compose (confirm_<slot> fragment). The renderer appends the
                # canonical re-ask. Record _pending_confirm so the NEXT
                # turn's gated repair counter can detect a failed confirm.
                _gate_blocked_writes = [
                    c.name for c in apply_commands
                    if c.command == "set_slot" and c.name
                ]
                frag_id = _gate_verdict.get("confirm_fragment_id")
                _confirm_slot = _gate_verdict.get("confirm_slot") or sot_awaiting_slot
                _confirm_value = _gate_verdict.get("confirm_value")
                if not _confirm_value:
                    for _c in apply_commands:
                        if _c.command == "set_slot" and _c.name:
                            _confirm_slot = _confirm_slot or _c.name
                            _confirm_value = _c.value
                            break
                from app.engine.fragment_library import get_fragment, resolve_confirm_fragment
                _candidate_date = None
                for _dc in apply_commands:
                    if (
                        _dc.command == "set_slot"
                        and _dc.name == "committed_date"
                        and str(_dc.value or "").strip()
                    ):
                        _candidate_date = str(_dc.value).strip()
                        break
                if not _candidate_date:
                    _cd_state = state.slots.get("committed_date")
                    if _cd_state:
                        _candidate_date = str(_cd_state).strip()
                _resolved_frag = resolve_confirm_fragment(
                    request.tenant_id,
                    _confirm_slot,
                    _confirm_value,
                    committed_date=_candidate_date,
                )
                if _resolved_frag:
                    frag_id = _resolved_frag
                elif frag_id and not get_fragment(request.tenant_id, str(frag_id)):
                    if _confirm_slot:
                        frag_id = f"confirm_{_confirm_slot}"
                if frag_id:
                    _gate_verdict["confirm_fragment_id"] = frag_id
                confirm_cmd: Command | None = None
                if frag_id:
                    confirm_cmd = Command(
                        command="compose",
                        fragments=[frag_id],
                        oof_class="payment_assertion",
                    )
                    # W2-4 enforce: render the confirm-ask fragment NOW so the
                    # reply assembly (later ``elif compose_fired`` branch) speaks
                    # it. Without this the gate blocks the write but the bot
                    # re-asks intent silently — the borrower never hears the
                    # confirm-readback, repeats the assertion, and the repair
                    # counter escalates. The gate is the final authority on
                    # money-state writes, so its confirm fragment overrides any
                    # earlier compose selection.
                    try:
                        _render_slots = dict(state.slots)
                        if _candidate_date:
                            _render_slots["committed_date"] = _candidate_date
                        compose_reply_text = render_compose(
                            request.tenant_id,
                            [frag_id],
                            _render_slots,
                            persona_voice=getattr(profile, "voice_id", None) if profile else None,
                        )
                        compose_fired = True
                        compose_fragment_ids = [frag_id]
                    except Exception:  # pragma: no cover — render fallback
                        compose_fired = False
                        compose_reply_text = ""
                else:
                    # No confirm fragment id → pure hold (re-ask only).
                    compose_fired = False
                    compose_reply_text = ""
                apply_commands = (
                    [confirm_cmd] if confirm_cmd else []
                )
                # E2: arm _pending_confirm ONLY when a real confirm fragment
                # rendered and will be spoken. A downgrade with no fragment
                # (e.g. start_flow cost miss before E1) must NOT plant a
                # phantom pending on the collect slot — that is what made
                # dc4c5808 t4 treat "हाँ। ऑफिस कहाँ है?" as a payment confirm.
                _confirm_spoke = bool(
                    frag_id and (compose_reply_text or "").strip()
                )
                if _confirm_spoke:
                    from app.engine.robustness import set_pending_confirm
                    state = set_pending_confirm(
                        state,
                        slot=str(_confirm_slot or frag_id),
                        fragment_id=frag_id,
                        value=str(_confirm_value) if _confirm_value is not None else None,
                        committed_date=_candidate_date,
                    )
            elif _gate_verdict["verdict"] == "hold":
                # Non-addressed or PII-locked: drop all candidate writes
                # (no slot write, no flow advance). The renderer re-asks.
                _gate_blocked_writes = [
                    c.name for c in apply_commands
                    if c.command == "set_slot" and c.name
                ]
                apply_commands = []
                # W2-4: the prior confirm-ask (if any) is moot — clear it
                # so the next turn doesn't misread it as a pending confirm.
                if _prior_pending_confirm:
                    _slots = dict(state.slots)
                    _slots.pop(PENDING_CONFIRM_KEY, None)
                    state = state.model_copy(deep=True)
                    state.slots = _slots
            elif _gate_verdict["verdict"] == "execute":
                # W2-4: confirm succeeded (evidence >= cost) OR no confirm
                # was pending — clear any prior _pending_confirm so the next
                # turn's score_evidence doesn't treat a yes-token as an
                # explicit confirm of a stale confirm-ask.
                # E3: question-shape keeps pending_confirm armed — we
                # answered the question first and will re-ask the confirm.
                if _prior_pending_confirm and not _question_shape:
                    _slots = dict(state.slots)
                    _slots.pop(PENDING_CONFIRM_KEY, None)
                    state = state.model_copy(deep=True)
                    state.slots = _slots
                # P4: persist locked refuse so a re-refusal after slot-clear
                # skips a second confirm and goes to attempt-2.
                _lock = dict(state.slots.get(LOCKED_SLOT_VALUES_KEY) or {})
                _refused = {"refused", "refuse", "unwilling", "denied", "no"}
                _changed = False
                for _c in apply_commands:
                    if _c.command != "set_slot" or not _c.name:
                        continue
                    _cv = str(_c.value or "").strip().lower()
                    if _cv in _refused:
                        _lock[str(_c.name)] = "refused"
                        _changed = True
                    elif _cv in {"willing", "specific_date"}:
                        if _lock.pop(str(_c.name), None) is not None:
                            _changed = True
                if _changed:
                    _slots = dict(state.slots)
                    _slots[LOCKED_SLOT_VALUES_KEY] = _lock
                    state = state.model_copy(deep=True)
                    state.slots = _slots

        # W3-1 PTP policy — post-gate only (invariant #1). Accept writes
        # ptp_date/ptp_amount source=confirmed + PTP_SET. Counter speaks
        # ptp_counter_date and arms a one-shot pending. Partial persist
        # offered_amount after the gate.
        if profile is not None:
            from app.engine.ptp_policy import apply_ptp_after_gate, compute_derived_slots

            _ptp_result = apply_ptp_after_gate(
                apply_commands=apply_commands,
                slots=dict(state.slots),
                transcript=request.transcript,
                profile=profile,
                today=_today,
                gate_verdict=_gate_verdict["verdict"],
                pending_confirm=_prior_pending_confirm if isinstance(_prior_pending_confirm, dict) else None,
                question_shape=_question_shape,
            )
            apply_commands = _ptp_result["commands"]
            _ptp_updates = dict(_ptp_result.get("slot_updates") or {})
            if _ptp_partial and _ptp_partial.get("offered_amount") is not None:
                _ptp_updates["offered_amount"] = _ptp_partial["offered_amount"]
                if _ptp_partial.get("remaining_after") is not None:
                    _ptp_updates["remaining_after"] = _ptp_partial["remaining_after"]
            _ptp_updates.update(compute_derived_slots({**state.slots, **_ptp_updates}, _today))
            if _ptp_updates:
                _slots = dict(state.slots)
                _slots.update(_ptp_updates)
                state = state.model_copy(deep=True)
                state.slots = _slots
            _ptp_fid = _ptp_result.get("compose_id")
            if _ptp_fid:
                _overlay = dict(_compose_slots)
                _overlay.update(_ptp_result.get("render_overlay") or {})
                _overlay.update(_ptp_updates)
                try:
                    compose_reply_text = render_compose(
                        request.tenant_id,
                        [_ptp_fid],
                        _overlay,
                        persona_voice=getattr(profile, "voice_id", None) if profile else None,
                    )
                    compose_fired = True
                    compose_fragment_ids = [_ptp_fid]
                except Exception:
                    pass
                if _ptp_result.get("pending_counter"):
                    from app.engine.robustness import set_pending_confirm

                    state = set_pending_confirm(
                        state,
                        slot=str(sot_awaiting_slot or "plo_payment_intent"),
                        fragment_id=_ptp_fid,
                        value="willing",
                        committed_date=str(
                            (_ptp_result.get("render_overlay") or {}).get("counter_date")
                            or ""
                        ) or None,
                    )

        with StageTimer(latency, "tracker_apply"):
            state = apply(state, [turn_event, *apply_commands])

        with StageTimer(latency, "priority_reorder"):
            state = apply_identity_entry_gate(state, flows)
            state = defer_collection_flows(state, flows)
            state = reorder(state, flows)

        with StageTimer(latency, "decision_overlay"):
            state = apply_decision_overlay(state, flows)

        action_runner = make_async_action_runner(tools)
        with span("executor"):
            with StageTimer(latency, "executor"):
                exec_result = await run_executor_async(state, flows, action_runner)
                state = exec_result.state

        # Warm transfer (orchestrator-only; the legacy voip.ivrobd.com POST is
        # REMOVED — it was dead, 404 in live testing). A transfer_call step set
        # transfer_requested; launch the detached driver exactly once: dial the
        # agent -> three-way on answer -> drop the AI leg (transfer/complete).
        # Requires ORCHESTRATOR_BASE_URL and a Stasis-owned call (the session
        # id resolves in the orchestrator's inbound registry). Not configured =
        # stub: log intent only; the action already set end_call in that case,
        # so the call ends cleanly, exactly like the old stub mode.
        if state.slots.get("transfer_requested") and not state.slots.get(
            "transfer_initiated"
        ):
            target = str(
                state.slots.get("transfer_target")
                or tenant_cfg.transfer_agent_number
                or settings.transfer_agent_number
            )
            reason = str(state.slots.get("transfer_reason") or "handoff")
            orchestrator_url = (os.getenv("ORCHESTRATOR_BASE_URL") or "").strip()
            if orchestrator_url and target:
                no_answer_reply = (
                    tenant_cfg.transfer_no_answer_reply.strip()
                    or settings.transfer_no_answer_reply
                )
                task = asyncio.create_task(
                    _drive_warm_transfer(
                        int(getattr(settings, "transfer_hold_ms", 0) or 0) / 1000.0,
                        session_uuid=state.call_id,
                        target=target,
                        caller_id=transfer_caller_id(state.slots.get("caller_id")),
                        reason=reason,
                        answer_budget_s=float(
                            getattr(settings, "transfer_answer_budget_s", 30.0) or 30.0
                        ),
                        complete_delay_s=int(
                            getattr(settings, "transfer_complete_delay_ms", 0) or 0
                        )
                        / 1000.0,
                        no_answer_reply=no_answer_reply,
                        end_call_grace_ms=settings.end_call_grace_ms,
                    )
                )
                _TRANSFER_TASKS.add(task)
                task.add_done_callback(_TRANSFER_TASKS.discard)
                state.slots["transfer_initiated"] = True
                state.slots["transfer_status"] = "pending"
                state.slots["disposition"] = "TRANSFER_PENDING"
            else:
                logger.info(
                    "transfer STUB call_id=%s target=%s reason=%s "
                    "(orchestrator not configured or no agent number)",
                    state.call_id,
                    target,
                    reason,
                )
                state.slots["transfer_initiated"] = True
                state.slots["transfer_status"] = "stub"
                state.slots["disposition"] = "TRANSFER_PENDING"

        # Live WhatsApp send. A send_whatsapp_message step set whatsapp_requested +
        # captured phone/name; fire the templated message exactly once here. Detached so
        # the closing line's TTS isn't delayed by the HTTP call. Stub mode already
        # "sent" (logged) in the action, so this only does work when live.
        if (
            state.slots.get("whatsapp_requested")
            and not state.slots.get("whatsapp_sent")
            and (getattr(settings, "whatsapp_mode", "stub") or "stub").lower() == "live"
            and getattr(settings, "whatsapp_endpoint_url", "")
        ):
            wa_phone = str(
                state.slots.get("whatsapp_phone")
                or state.slots.get("phone")
                or state.slots.get("borrower_phone")
                or ""
            )
            wa_name = str(
                state.slots.get("whatsapp_name")
                or state.slots.get("customer_name")
                or state.slots.get("borrower_name")
                or ""
            )
            wa_task = asyncio.create_task(
                _send_whatsapp_bg(phone=wa_phone, name=wa_name)
            )
            _WHATSAPP_TASKS.add(wa_task)
            wa_task.add_done_callback(_WHATSAPP_TASKS.discard)
            state.slots["whatsapp_sent"] = True

        # Conversation repair (F1): count consecutive re-asks of the same slot and,
        # once the retry cap is hit, hand off gracefully instead of looping.
        # Routing misses must not burn borrower retries: Layer-3 weak-jump suppression
        # (legacy digression) or Tier-2 out-of-catalog start_flow rejects.
        catalog_jump_rejected = catalog_mode and any(
            "out-of-catalog" in r for r in command_rejections
        )
        had_inbound = bool((request.transcript or "").strip())
        # W2-4 enforce-coupled repair counter: in enforce mode, increment
        # ONLY on failed confirms (prior turn issued a confirm-ask AND this
        # turn's evidence < 3). routing_miss / agent_fault become reasons,
        # not skip conditions. In shadow, the legacy track_slot_reask runs
        # unchanged (behaviour preserved).
        if _gate_enforce:
            from app.engine.robustness import track_slot_reask_gated
            state, repair_escalate, _repair_reason = track_slot_reask_gated(
                state,
                question_slot=exec_result.question_slot,
                had_inbound=had_inbound,
                max_retries=tenant_cfg.max_slot_retries,
                evidence_score=int(_evidence.get("evidence", 0) or 0),
                routing_miss=(
                    weak_jump_suppressed or catalog_jump_rejected or respond_fired
                ),
                prior_pending_confirm=_prior_pending_confirm,
                question_shape=_question_shape,
            )
        else:
            state, repair_escalate = track_slot_reask(
                state,
                question_slot=exec_result.question_slot,
                had_inbound=had_inbound,
                max_retries=tenant_cfg.max_slot_retries,
                routing_miss=(
                    weak_jump_suppressed or catalog_jump_rejected or respond_fired
                ),
            )
            _repair_reason = None

        grounding_result: str | None = None
        with StageTimer(latency, "nlg"):
            flows_eff, pack_rejected, pack_rejected_reason = _resolve_effective_flows(
                flows,
                brand_pack,
            )
            if repair_escalate or frustration_escalate:
                resolved = ResolvedReply(
                    text=tenant_cfg.escalation_reply,
                    reply_id="repair_escalation",
                )
                state = mark_repair_escalation(
                    state, question_slot=exec_result.question_slot
                )
                if frustration_escalate and not repair_escalate:
                    state.slots["disposition"] = FRUSTRATION_ESCALATION_DISPOSITION
            elif compose_fired and compose_reply_text:
                # W2-3 compose lane: the rendered fragment text IS the
                # reply. Append the canonical re-ask (short variant) — EXACT
                # RESUME append, never TTS-buffer replay. The renderer
                # already gender-resolved + slot-filled; here we just append
                # the pending collect's short re-ask.
                reask_slot = (
                    exec_result.question_slot
                    or sot_awaiting_slot
                    or state.slots.get("last_question_slot")
                )
                reask = render_short_reask(
                    str(reask_slot or ""),
                    state,
                    flows_eff,
                    locale=request.locale,
                    channel=request.channel,
                    tenant_cfg=tenant_cfg,
                )
                draft = f"{compose_reply_text} {reask.text}".strip()
                resolved = ResolvedReply(
                    text=draft,
                    reply_id="compose",
                    variant_index=reask.variant_index,
                    language=reask.language,
                    tone_register=reask.tone_register,
                )
            elif respond_fired:
                # Fact-ground then append short re-ask of the pending collect.
                grounded, grounding_result = ground_respond_text(
                    respond_text_raw,
                    state.slots,
                    unknown_info_reply,
                )
                reask_slot = (
                    exec_result.question_slot
                    or sot_awaiting_slot
                    or state.slots.get("last_question_slot")
                )
                reask = render_short_reask(
                    str(reask_slot or ""),
                    state,
                    flows_eff,
                    locale=request.locale,
                    channel=request.channel,
                    tenant_cfg=tenant_cfg,
                )
                draft = f"{grounded} {reask.text}".strip()
                resolved = ResolvedReply(
                    text=draft,
                    reply_id=reask.reply_id or "respond",
                    variant_index=reask.variant_index,
                    language=reask.language,
                    tone_register=reask.tone_register,
                )
            else:
                resolved = draft_reply_resolved(
                    reply_id=exec_result.reply_id,
                    question_slot=exec_result.question_slot,
                    commands=apply_commands,
                    state=state,
                    flows=flows_eff,
                    tenant_cfg=tenant_cfg,
                    locale=request.locale,
                    channel=request.channel,
                    transfer_to_human=exec_result.transfer_to_human,
                    utter_chain=exec_result.utter_chain,
                )
            draft = resolved.text
            # E3: after answering the question, re-speak the pending
            # confirm-ask so the borrower can still confirm money-state.
            if (
                _question_shape
                and isinstance(_prior_pending_confirm, dict)
                and _prior_pending_confirm.get("fragment_id")
            ):
                try:
                    _reconfirm = render_compose(
                        request.tenant_id,
                        [str(_prior_pending_confirm["fragment_id"])],
                        dict(state.slots),
                        persona_voice=getattr(profile, "voice_id", None) if profile else None,
                    )
                    if _reconfirm and _reconfirm not in draft:
                        draft = f"{draft} {_reconfirm}".strip()
                        resolved = ResolvedReply(
                            text=draft,
                            reply_id=resolved.reply_id,
                            variant_index=resolved.variant_index,
                            language=resolved.language,
                            tone_register=resolved.tone_register,
                        )
                except Exception:
                    pass
            state = record_outbound_context(
                state,
                reply_id=exec_result.reply_id or resolved.reply_id,
                question_slot=exec_result.question_slot,
                draft=draft,
            )

        with span("gate"):
            with StageTimer(latency, "gate"):
                # Gate runs on COMBINED (respond + re-ask) draft, never respond alone.
                reply_text, state, transfer, audit_chain = process_outbound_reply(
                    draft,
                    state,
                    request,
                    candidate_flows=candidate_flows,
                    commands=commands_payload,
                    actions_called=exec_result.actions_called,
                    latency=latency,
                    llm_calls=llm_calls,
                    resolved=resolved,
                    manifest_version=MANIFEST_VERSION,
                    brand_pack=brand_pack,
                    pack_rejected=pack_rejected,
                    pack_rejected_reason=pack_rejected_reason,
                )

        # HARDEN-1 F3(b): record whether this turn's final gated reply was
        # empty/failed so the NEXT turn's track_slot_reask can skip the
        # repair-counter increment (the re-ask is the agent's fault, not the
        # borrower's). Must run AFTER the gate produces reply_text and BEFORE
        # the persist so the flag is durable across turns.
        state = record_agent_fault(state, reply_text=reply_text)

        # W2-1: persist the final spoken reply + last borrower transcript so the
        # NEXT turn's echo filter + evidence scorer can read them. Written AFTER
        # the gate (no side-effect before gate — invariant #1) and BEFORE persist
        # so the values are durable. Telemetry-only slots (underscore-prefixed).
        state.slots["last_spoken_reply"] = reply_text or ""
        state.slots["_last_borrower_transcript"] = request.transcript or ""
        # W2-4b D2: persist classification cache AFTER the gate (underscore
        # telemetry slot — same band as last_spoken_reply).
        from app.engine.command_gen import parse_validate_success
        if (
            llm_calls >= 1
            and _class_key
            and parse_validate_success(parse_result)
        ):
            persisted = dict(_class_cache)
            persisted[_class_key] = parse_result.raw
            if len(persisted) > 32:
                persisted = dict(list(persisted.items())[-32:])
            state.slots["_cmd_class_cache"] = persisted

        # W2-3 diversion ladder (own counter, separate from repair —
        # invariant #9). Increment on irrelevant / repeated_diversion turns;
        # reset on any on-rail turn. 3rd diversion → callback/graceful exit
        # (the executor / policy preempt path handles the exit; this counter
        # is the signal). Policy preempts always preempt (invariant #2) —
        # they run before the gate and are not diversion turns.
        if parse_result.oof_class in ("irrelevant", "repeated_diversion"):
            state.slots["_redirect_count"] = int(state.slots.get("_redirect_count") or 0) + 1
        else:
            state.slots["_redirect_count"] = 0

        # W2-1 evidence score was computed pre-executor (before the Commitment
        # Gate) using ``sot_awaiting_slot`` (the slot the prior turn asked —
        # the slot the borrower is answering this turn). Reuse that value
        # here for the guards log; do not recompute. See the gate call site
        # above for the rationale.

        log_turn_decision(
            session_id=request.call_id,
            transcript=request.transcript,
            borrower=borrower,
            kb_candidates=candidate_flows,
            commands=commands,
            rejected_slots=command_rejections,
            state=state,
            reply_id=exec_result.reply_id or resolved.reply_id,
            gate_verdict=audit_chain.gate_verdict,
            gate_reason=audit_chain.gate_reason,
            draft_reply=draft,
            final_reply=reply_text,
            raw_llm=parse_result.raw,
            question_slot=exec_result.question_slot,
            guards={
                "dispute_evidence": state.slots.get(DISPUTE_EVIDENCE_KEY) or {},
                "dispute_forced": dispute_forced,
                "frustration_turns": state.slots.get(FRUSTRATION_COUNT_KEY) or 0,
                "frustration_escalate": frustration_escalate,
                "repair_escalate": repair_escalate,
                "label_transition": (
                    label_decision.model_dump(mode="json") if label_decision else None
                ),
                "respond_fired": respond_fired,
                "grounding_result": grounding_result,
                "final_text_len": len(reply_text or ""),
                "gate_warnings": list(audit_chain.gate_warnings or []),
                "refusal_matched_via": coercion_meta.get("refusal_matched_via"),
                "refusal_class": coercion_meta.get("refusal_class"),
                "alias_used": list(parse_result.alias_used or []),
                "echo_suspected": False,
                "evidence": _evidence["evidence"],
                "evidence_reason": _evidence["evidence_reason"],
                "evidence_signals": _evidence["evidence_signals"],
                # W2-2 Commitment Gate (SHADOW): verdict + would_downgrade +
                # confirm_fragment_id logged per turn. Behaviour unchanged
                # this phase (enforce=false). Enforce flip ships after the
                # shadow observation week.
                "gate_verdict": _gate_verdict["verdict"],
                "gate_reason": _gate_verdict["reason"],
                "gate_cost_class": _gate_verdict["cost_class"],
                "gate_max_cost": _gate_verdict["max_cost"],
                "would_downgrade": _gate_verdict["would_downgrade"],
                "confirm_fragment_id": _gate_verdict["confirm_fragment_id"],
                "gate_enforce": _gate_enforce,
                # W2-4 enforce-coupled: blocked slot writes (downgrade/hold)
                # + repair reason (failed_confirm / routing_miss / agent_fault).
                "gate_blocked_writes": _gate_blocked_writes,
                "repair_reason": _repair_reason,
                # W2-3 router contract (same LLM call, invariant #7).
                # oof_class (9 values) + subclass + secondary_intents +
                # confidence (telemetry-only, invariant #6). None on normal
                # turns (parse-surface discipline).
                "oof_class": parse_result.oof_class,
                "oof_subclass": parse_result.oof_subclass,
                "secondary_intents": list(parse_result.secondary_intents or []),
                "llm_confidence": parse_result.confidence,
                # W2-3 compose lane telemetry.
                "compose_fired": compose_fired,
                "compose_fragment_ids": compose_fragment_ids,
                "compose_rejections": compose_rejections,
                "unrelated_redirect": unrelated_redirect,
                # W2-3 Tier-3 demotion (invariant #4): respond is the escape
                # hatch — fires only when compose misses. escape_hatch_used
                # logged; target <5% of OOF turns.
                "escape_hatch_used": escape_hatch_used,
                # W2-3 complaint class → ack+grievance + complaint_raised.
                "complaint_raised": parse_result.oof_class == "complaint",
                # W2-3 diversion ladder (own counter, separate from repair).
                # redirect_count = consecutive irrelevant/repeated_diversion
                # turns; 3rd → callback/graceful exit (policy preempts always
                # preempt).
                "redirect_count": int(state.slots.get("_redirect_count") or 0),
                # W2-4b LLM-diet telemetry.
                "cue_hit_skip": cue_hit_skip,
                "cue_pack": cue_pack,
                "class_cache_hit": class_cache_hit,
                "scope_miss": parse_result.scope_miss,
                "catalog_scoped_count": len(candidate_flows),
                "ptp_verdict": (
                    (_ptp_result.get("verdict").action if _ptp_result and _ptp_result.get("verdict") else None)
                    or (_ptp_partial.get("verdict").action if _ptp_partial and _ptp_partial.get("verdict") else None)
                ),
                "outcome": "PROCEED",
            },
        )

        logger.info(
            "turn_latency %s",
            json.dumps(
                {"session_id": request.call_id, "llm_calls": llm_calls, **latency.to_dict()},
                default=str,
            ),
        )

        if on_gated_reply is not None:
            # Pass live slots (not yet persisted) so chunk frames can carry
            # voice_id / tts_model / tts_pace set by actions like select_plo_scenario.
            # Profile defaults cover the opener (before select_*_scenario runs).
            if profile is not None:
                if profile.voice_id:
                    state.slots.setdefault("voice_id", profile.voice_id)
                if profile.tts_model:
                    state.slots.setdefault("tts_model", profile.tts_model)
                if profile.tts_pace is not None and state.slots.get("tts_pace") is None:
                    state.slots["tts_pace"] = profile.tts_pace
            voice_id = state.slots.get("voice_id")
            tts_model = state.slots.get("tts_model")
            tts_pace_raw = state.slots.get("tts_pace")
            tts_pace: float | None = None
            if tts_pace_raw is not None and tts_pace_raw != "":
                try:
                    tts_pace = float(tts_pace_raw)
                except (TypeError, ValueError):
                    tts_pace = None
            await on_gated_reply(
                reply_text,
                voice_id=str(voice_id) if voice_id else None,
                tts_model=str(tts_model) if tts_model else None,
                tts_pace=tts_pace,
            )

        # Flow-exhaustion guard: on salary_on_time the whole call is script-driven, so an
        # empty flow stack at the end of a turn means nothing is left to follow (e.g. the
        # borrower cancelled/said bye and the LLM emitted cancel_flow). Rather than idle on
        # a generic clarify forever, mark the call closed and disconnect after this reply.
        # Persisting sot_call_closed also makes any late barge-in hit the terminal guard.
        force_end_no_flow = (
            profile is not None
            and not state.flow_stack
            and not (exec_result.end_call or repair_escalate)
        )
        if force_end_no_flow:
            closed_slot = profile.call_closed_slot or f"{profile.flow_prefix}call_closed"
            state.slots[closed_slot] = True
            state.slots["end_call"] = True
            from app.engine.nlg import clear_reply_counts

            clear_reply_counts(state.slots)
            if not state.slots.get("disposition"):
                state.slots["disposition"] = "CALL_ENDED_NO_FLOW"

        with StageTimer(latency, "persist"):
            audit_id = await _persist_turn(memory, state, borrower, request, audit_chain)

        # W1-B.4 (H2 dead-air defense): silence is always visible. Emit a
        # structured reply_empty log keyed by turn_id so a mute turn is
        # greppable in isolation (not buried inside the audit record). The
        # gate can legitimately produce an empty reply (e.g. a pure side-effect
        # turn), but it must never be silent in the logs.
        reply_empty = not (reply_text or "").strip()
        logger.info(
            "reply_empty=%s turn_id=%s call_id=%s tenant_id=%s reply_id=%s final_text_len=%d",
            reply_empty,
            audit_id,
            request.call_id,
            request.tenant_id,
            resolved.reply_id,
            len(reply_text or ""),
        )

        annotate_turn_span(
            turn_span,
            chain=audit_chain,
            latency=latency,
            llm_calls=llm_calls,
        )

        if (
            _ptp_result
            and _ptp_result.get("verdict")
            and getattr(_ptp_result["verdict"], "action", None) in {"accept", "accept_flagged"}
        ):
            state.slots["disposition"] = "PTP_SET"
        disposition = exec_result.disposition
        if disposition is None and state.slots.get("disposition") is not None:
            disposition = str(state.slots["disposition"])
        if state.slots.get("ptp_date") and state.slots.get("disposition") == "PTP_SET":
            disposition = "PTP_SET"
        if repair_escalate:
            disposition = "ESCALATED_UNCLEAR"

        # HARDEN-1 F3(a): both repair-limit and frustration-limit escalations must
        # carry end_call in the SAME turn. mark_repair_escalation already sets
        # slots["end_call"]=True, but the TurnResponse.end_call field is what the
        # go-server reads to hang up — frustration_escalate used to be missing from
        # this OR, so a frustration escalation replied then left a zombie turn
        # before the terminal guard caught the next barge-in. Either escalation
        # trigger ends the call now.
        return TurnResponse(
            reply_text=reply_text,
            end_call=(
                exec_result.end_call
                or repair_escalate
                or frustration_escalate
                or force_end_no_flow
            ),
            transfer_to_human=transfer or exec_result.transfer_to_human,
            actions_executed=list(exec_result.actions_called),
            disposition=disposition,
            state_version=state.version,
            audit_id=audit_id,
            reply_id=resolved.reply_id,
            variant_index=resolved.variant_index,
            language=resolved.language,
            tone_register=resolved.tone_register,
        )
