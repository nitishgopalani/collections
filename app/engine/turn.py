"""Single-turn orchestration — full pipeline (Sprint 7)."""

import logging
import uuid
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.config import tenant_config
from app.engine.actions import make_async_action_runner
from app.engine.command_gen import generate
from app.engine.compliance_handoff import sync_compliance_notes_on_persist
from app.engine.executor import ExecResult
from app.engine.executor import run_async as run_executor_async
from app.engine.followup import hydrate_followup_from_borrower, sync_followup_on_persist
from app.engine.gate import gate
from app.engine.hardship import sync_hardships_on_persist
from app.engine.identity_gate import apply_identity_entry_gate, defer_collection_flows
from app.engine.latency import StageTimer, TurnLatencyProfile
from app.engine.nlg import draft_reply
from app.engine.priority import reorder
from app.engine.retrieval import retrieve_flow_candidates
from app.engine.robustness import record_outbound_context
from app.engine.safety import apply_safety_to_state, safety_preempt
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
from app.flows.loader import load_all_flows
from app.memory.audit import TurnAuditChain, build_turn_audit_record
from app.schemas.api import TurnRequest, TurnResponse
from app.schemas.command import Command
from app.schemas.state import BorrowerRecord, ConversationState, Event
from app.telemetry import annotate_turn_span, span, turn_trace

logger = logging.getLogger(__name__)


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
    candidate_flows: list[dict] | None = None,
    commands: list[dict] | None = None,
    actions_called: list[str] | None = None,
    safety_reason: str | None = None,
    latency: TurnLatencyProfile | None = None,
    llm_calls: int = 0,
    now: datetime | None = None,
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
    latency_data = latency.to_dict() if latency is not None else {}
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
        final_reply=gate_result.text,
        transfer_to_human=transfer or gate_result.transfer_to_human,
        latency_ms=latency_data.get("stages", {}),
        engine_internal_ms=float(latency_data.get("engine_internal_ms", 0.0)),
        external_ms=float(latency_data.get("external_ms", 0.0)),
        llm_calls=llm_calls,
    )
    return gate_result.text, updated, chain.transfer_to_human, chain


def safety_check_transcript(
    request: TurnRequest,
    state: ConversationState,
) -> tuple[ConversationState, str | None]:
    """Run safety pre-empt; return updated state and optional early reply text."""
    tenant_cfg = tenant_config(request.tenant_id)
    safety = safety_preempt(
        request.transcript,
        state,
        tenant_cfg,
        emotion_label=state.slots.get("emotion"),
        emotion_intensity=state.slots.get("emotion_intensity"),
    )
    if safety is None:
        return state, None
    updated = apply_safety_to_state(state, safety)
    return updated, safety.reply_text


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


async def _run_safety_early_exit(
    request: TurnRequest,
    state: ConversationState,
    borrower: BorrowerRecord,
    memory: Any,
    latency: TurnLatencyProfile,
    turn_span: Any,
    llm_calls: int,
    safety_reply: str,
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

    reply_text, state, transfer, audit_chain = process_outbound_reply(
        safety_reply,
        state,
        request,
        safety_reason="safety_preempt",
        latency=latency,
        llm_calls=llm_calls,
    )

    with StageTimer(latency, "persist"):
        audit_id = await _persist_turn(memory, state, borrower, request, audit_chain)

    annotate_turn_span(turn_span, chain=audit_chain, latency=latency, llm_calls=llm_calls)
    return TurnResponse(
        reply_text=reply_text,
        end_call=False,
        transfer_to_human=transfer,
        actions_executed=[],
        disposition=None,
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
) -> TurnResponse:
    """Full turn loop: safety → retrieval → command_gen → executor → nlg → gate → persist."""
    latency = TurnLatencyProfile()
    llm_calls = 0
    flows = load_all_flows()
    tenant_cfg = tenant_config(request.tenant_id)

    with turn_trace(request.call_id, request.borrower_id, request.tenant_id) as turn_span:
        with StageTimer(latency, "load_state"):
            state = await memory.load_state(request.call_id)
            if state is None:
                state = new_conversation_state(
                    request.call_id,
                    request.tenant_id,
                    request.borrower_id,
                )
            borrower = await memory.load_borrower(request.borrower_id)
            if borrower is None:
                borrower = BorrowerRecord(borrower_id=request.borrower_id)
            state = hydrate_from_borrower(state, borrower)
            state = hydrate_followup_from_borrower(state, borrower)
            state = apply_trust_to_state(state, borrower)
            state = apply_risk_to_state(state, borrower)
            state = apply_persona_to_state(state, borrower)
            state = apply_recovery_to_state(state, borrower)
            if request.turn_meta.get("call_date"):
                state.slots["call_date"] = request.turn_meta["call_date"]

            emotion = classify_emotion_from_turn(
                request.transcript,
                turn_meta=request.turn_meta,
                channel=request.channel,
            )
            state = apply_emotion_to_state(state, emotion)

            state = apply_identity_entry_gate(state, flows)

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
            )

        state.attempts += 1

        candidate_flows: list[dict[str, Any]] = []
        commands: list[Command] = []
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

        with span("retrieval", external=True):
            with StageTimer(latency, "retrieval", external=True):
                candidates = await retrieve_flow_candidates(
                    kb,
                    request.transcript,
                    request.tenant_id,
                )
        candidate_flows = [
            {"name": c.name, "description": c.description, "score": c.score}
            for c in candidates
        ]

        with span("command_gen", external=True):
            with StageTimer(latency, "command_gen", external=True):
                commands = await generate(
                    request.transcript,
                    state,
                    candidate_flows,
                    llm=llm,
                )
                llm_calls = 1

        commands_payload = [cmd.model_dump(mode="json") for cmd in commands]

        with StageTimer(latency, "tracker_apply"):
            state = apply(state, [turn_event, *commands])

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

        with StageTimer(latency, "nlg"):
            draft = draft_reply(
                reply_id=exec_result.reply_id,
                question_slot=exec_result.question_slot,
                commands=commands,
                state=state,
                flows=flows,
                tenant_cfg=tenant_cfg,
                locale=request.locale,
                channel=request.channel,
                transfer_to_human=exec_result.transfer_to_human,
            )
            state = record_outbound_context(
                state,
                reply_id=exec_result.reply_id,
                question_slot=exec_result.question_slot,
                draft=draft,
            )

        with span("gate"):
            with StageTimer(latency, "gate"):
                reply_text, state, transfer, audit_chain = process_outbound_reply(
                    draft,
                    state,
                    request,
                    candidate_flows=candidate_flows,
                    commands=commands_payload,
                    actions_called=exec_result.actions_called,
                    latency=latency,
                    llm_calls=llm_calls,
                )

        with StageTimer(latency, "persist"):
            audit_id = await _persist_turn(memory, state, borrower, request, audit_chain)

        annotate_turn_span(
            turn_span,
            chain=audit_chain,
            latency=latency,
            llm_calls=llm_calls,
        )

        disposition = exec_result.disposition
        if disposition is None and state.slots.get("disposition") is not None:
            disposition = str(state.slots["disposition"])

        return TurnResponse(
            reply_text=reply_text,
            end_call=exec_result.end_call,
            transfer_to_human=transfer or exec_result.transfer_to_human,
            actions_executed=list(exec_result.actions_called),
            disposition=disposition,
            state_version=state.version,
            audit_id=audit_id,
        )
