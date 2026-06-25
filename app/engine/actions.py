"""Governed tool action layer (Sprint 3)."""

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from typing import Any

from app.engine.compliance_handoff import (
    classify_third_party_response as classify_third_party,
)
from app.engine.compliance_handoff import (
    normalize_third_party_contact,
)
from app.engine.hardship import (
    HARDSHIP_REASON_LABELS,
    build_hardship_record,
    normalize_hardship_reason,
)
from app.exceptions import ToolInvocationError
from app.schemas.state import ConversationState, Event, Frame

logger = logging.getLogger(__name__)

READ_TOOLS = frozenset({"check_last_payment", "get_balance", "get_borrower", "verify_identity"})
WRITE_TOOLS = frozenset(
    {"create_payment_link", "raise_dispute_ticket", "schedule_followup", "log_disposition"}
)

ACTION_TO_TOOL: dict[str, str] = {
    "verify_payment": "check_last_payment",
    "verify_identity": "verify_identity",
    "lookup_dues_breakup": "get_balance",
    "create_payment_link": "create_payment_link",
    "raise_dispute_ticket": "raise_dispute_ticket",
    "schedule_followup": "schedule_followup",
    "log_disposition": "log_disposition",
}

LOCAL_ACTIONS = frozenset(
    {
        "validate_ptp",
        "validate_partial",
        "push_ptp_for_balance",
        "set_partial_disposition",
        "set_payment_confirmed_disposition",
        "route_to_dispute",
        "validate_hardship_reason",
        "apply_hardship_empathy",
        "write_hardship_record",
        "route_hardship_partial",
        "route_hardship_forbearance",
        "route_hardship_review",
        "set_hardship_disposition",
        "mark_vague_ptp",
        "check_hardship_for_vague_ptp",
        "route_to_hardship",
        "push_specify_ptp",
        "route_vulnerable",
        "evaluate_resume",
        "drop_dispute_resume_parent",
        "drop_for_payment_found",
        "set_identity_ok",
        "incr_identity_attempts",
        "route_identity_failure",
        "apply_opt_out",
        "activate_third_party_mode",
        "classify_third_party_response",
        "confirm_not_borrower",
        "route_minor_handoff",
        "halt_fraud_handoff",
        "halt_lawyer_handoff",
        "halt_deceased_handoff",
        "halt_incapacitated_handoff",
        "log_harassment_complaint",
    }
)


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    return None


def _call_today(state: ConversationState) -> date:
    raw = state.slots.get("call_date") or state.slots.get("today")
    parsed = _parse_date(raw)
    if parsed is not None:
        return parsed
    return date.today()


def _idempotency_key(call_id: str, action: str, args: dict[str, Any]) -> str:
    payload = json.dumps(args, sort_keys=True, default=str)
    return hashlib.sha256(f"{call_id}:{action}:{payload}".encode()).hexdigest()


def _parse_amount(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _link_amount(state: ConversationState) -> Any:
    slots = state.slots
    return slots.get("link_amount") or slots.get("partial_amount") or slots.get("amount_due")


def _tool_args(action: str, state: ConversationState) -> dict[str, Any]:
    slots = state.slots
    base = {
        "borrower_id": state.borrower_id,
        "loan_id": slots.get("loan_id"),
    }
    if action == "verify_payment":
        return base
    if action == "verify_identity":
        return {**base, "identity_response": slots.get("identity_response")}
    if action == "create_payment_link":
        args = {**base, "amount": _link_amount(state)}
        rail = slots.get("payment_rail")
        if rail is not None:
            args["rail"] = rail
        return args
    if action == "raise_dispute_ticket":
        return {**base, "reason": slots.get("dispute_reason")}
    if action == "schedule_followup":
        return {**base, "followup_date": slots.get("ptp_date")}
    if action == "log_disposition":
        return {**base, "disposition": slots.get("disposition")}
    return base


class ActionRegistry:
    """Maps flow action names to governed ToolClient invocations."""

    def __init__(self, tools: Any) -> None:
        self._tools = tools
        self._read_cache: dict[str, dict[str, Any]] = {}
        self._turn_marker: tuple[str, int] | None = None

    def begin_turn(self, call_id: str, version: int) -> None:
        self._read_cache.clear()
        self._turn_marker = (call_id, version)

    def _ensure_turn(self, state: ConversationState) -> None:
        marker = (state.call_id, state.version)
        if self._turn_marker != marker:
            self.begin_turn(state.call_id, state.version)

    async def run_async(self, action: str, state: ConversationState) -> ConversationState:
        self._ensure_turn(state)

        if action in LOCAL_ACTIONS:
            return self._run_local(action, state)

        tool_name = ACTION_TO_TOOL.get(action)
        if tool_name is None:
            raise KeyError(f"Unknown action: {action}")

        if tool_name in READ_TOOLS:
            return await self._run_read(action, tool_name, state)
        if tool_name in WRITE_TOOLS:
            return await self._run_write(action, tool_name, state)
        raise KeyError(f"No tool mapping for action: {action}")

    def run(self, action: str, state: ConversationState) -> ConversationState:
        return asyncio.run(self.run_async(action, state))

    async def _run_read(
        self,
        action: str,
        tool_name: str,
        state: ConversationState,
    ) -> ConversationState:
        args = _tool_args(action, state)
        cache_key = f"{tool_name}:{json.dumps(args, sort_keys=True, default=str)}"
        if cache_key not in self._read_cache:
            try:
                response = await self._invoke_read_with_retry(tool_name, args, state)
            except ToolInvocationError as exc:
                return self._apply_tool_failure(state, action, str(exc))
            self._read_cache[cache_key] = response
        response = self._read_cache[cache_key]
        return self._apply_tool_result(action, tool_name, state, response, args)

    async def _run_write(
        self,
        action: str,
        tool_name: str,
        state: ConversationState,
    ) -> ConversationState:
        args = _tool_args(action, state)
        key = _idempotency_key(state.call_id, action, args)
        try:
            response = await self._tools.invoke(
                tool_name,
                args,
                state.tenant_id,
                idempotency_key=key,
            )
        except ToolInvocationError as exc:
            return self._apply_tool_failure(state, action, str(exc))
        return self._apply_tool_result(action, tool_name, state, response, args)

    async def _invoke_read_with_retry(
        self,
        tool_name: str,
        args: dict[str, Any],
        state: ConversationState,
    ) -> dict[str, Any]:
        last_error: ToolInvocationError | None = None
        for _ in range(2):
            try:
                return await self._tools.invoke(tool_name, args, state.tenant_id)
            except ToolInvocationError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise ToolInvocationError("read tool failed")

    def _apply_tool_failure(
        self,
        state: ConversationState,
        action: str,
        message: str,
    ) -> ConversationState:
        updated = state.model_copy(deep=True)
        slots = dict(updated.slots)
        slots["tool_error"] = message
        slots["tool_failed"] = True
        slots["transfer_to_human"] = True
        updated.slots = slots
        updated.events.append(
            Event(
                ts=datetime.now(UTC).isoformat(),
                kind="action",
                data={"action": action, "error": message, "tool_failed": True},
            )
        )
        return updated

    def _apply_tool_result(
        self,
        action: str,
        tool_name: str,
        state: ConversationState,
        response: dict[str, Any],
        args: dict[str, Any],
    ) -> ConversationState:
        updated = state.model_copy(deep=True)
        slots = dict(updated.slots)
        result = response.get("result", {}) if isinstance(response.get("result"), dict) else {}

        if action == "verify_payment":
            slots["payment_found"] = bool(result.get("found"))
            if result.get("found"):
                slots["last_payment_amount"] = result.get("amount")
                slots["last_payment_date"] = result.get("date")
                slots["last_payment_id"] = result.get("payment_id")
                status = str(result.get("status") or "posted")
                slots["payment_status"] = status
                slots["payment_processing"] = status in {"processing", "pending"}
        elif action == "lookup_dues_breakup":
            slots["principal"] = result.get("principal")
            slots["interest"] = result.get("interest")
            slots["charges"] = result.get("charges")
            slots["dues_breakup_loaded"] = True
            if result.get("amount_due") is not None:
                slots["amount_due"] = result.get("amount_due")
        elif action == "verify_identity":
            slots["identity_verified"] = bool(result.get("identity_verified"))
        elif action == "create_payment_link":
            slots["payment_link"] = result.get("payment_link")
            if result.get("rail") is not None:
                slots["payment_link_rail"] = result.get("rail")
        elif action == "raise_dispute_ticket":
            slots["dispute_logged"] = bool(result.get("dispute_logged"))
            slots["dispute_ticket_id"] = result.get("ticket_id")
        elif action == "schedule_followup":
            slots["followup_scheduled"] = bool(result.get("scheduled", True))
        elif action == "log_disposition":
            slots["disposition_logged"] = bool(result.get("logged", True))

        updated.slots = slots
        updated.events.append(
            Event(
                ts=datetime.now(UTC).isoformat(),
                kind="action",
                data={
                    "action": action,
                    "tool": tool_name,
                    "args": args,
                    "result": result,
                },
            )
        )
        return updated

    def _run_local(self, action: str, state: ConversationState) -> ConversationState:
        updated = state.model_copy(deep=True)
        slots = dict(updated.slots)

        if action == "validate_ptp":
            today = _call_today(updated)
            ptp_date = _parse_date(slots.get("ptp_date"))
            if ptp_date is None:
                slots["ptp_allowed"] = False
            else:
                days_out = (ptp_date - today).days
                max_days = int(slots.get("ptp_max_days") or 14)
                slots["ptp_allowed"] = 0 <= days_out <= max_days
        elif action == "validate_partial":
            partial = _parse_amount(slots.get("partial_amount"))
            amount_due = _parse_amount(slots.get("amount_due")) or 0.0
            if partial is None:
                slots["partial_valid"] = False
            else:
                valid = 0 < partial <= amount_due
                slots["partial_valid"] = valid
                if valid:
                    slots["link_amount"] = int(partial) if partial == int(partial) else partial
        elif action == "push_ptp_for_balance":
            partial = _parse_amount(slots.get("partial_amount")) or 0.0
            amount_due = _parse_amount(slots.get("amount_due")) or 0.0
            balance = max(amount_due - partial, 0.0)
            slots["balance_remaining"] = int(balance) if balance == int(balance) else balance
            slots["amount_due"] = slots["balance_remaining"]
            insert_at = max(len(updated.flow_stack) - 1, 0)
            updated.flow_stack.insert(
                insert_at,
                Frame(flow="promise_to_pay", step_index=0),
            )
        elif action == "set_partial_disposition":
            slots["disposition"] = "PARTIAL_CAPTURED"
        elif action == "set_payment_confirmed_disposition":
            slots["disposition"] = "PAYMENT_CONFIRMED"
            if slots.get("utr_reference"):
                slots["utr_captured"] = True
        elif action == "route_to_dispute":
            slots["routed_from_already_initiated"] = True
            if updated.flow_stack:
                updated.flow_stack.pop()
            updated.flow_stack.append(Frame(flow="dispute", step_index=0))
            slots["_skip_flow_pop"] = True
        elif action == "validate_hardship_reason":
            reason = normalize_hardship_reason(slots.get("hardship_reason"))
            if reason:
                slots["hardship_reason"] = reason
                slots["hardship_reason_valid"] = True
                slots["hardship_reason_label"] = HARDSHIP_REASON_LABELS.get(reason, reason)
            else:
                slots["hardship_reason_valid"] = False
        elif action == "apply_hardship_empathy":
            slots["tone_register"] = "reassure"
            slots["hardship_active"] = True
            slots["pressure_allowed"] = False
            persona = dict(slots.get("persona") or {})
            persona.update(
                {
                    "ability": "low",
                    "willingness": "high",
                    "primary_persona": "temporary_hardship",
                }
            )
            slots["persona"] = persona
        elif action == "write_hardship_record":
            slots["hardship_record_pending"] = build_hardship_record(updated)
        elif action == "route_hardship_partial":
            insert_at = max(len(updated.flow_stack) - 1, 0)
            updated.flow_stack.insert(insert_at, Frame(flow="partial_payment", step_index=0))
            slots["hardship_corroborated"] = True
        elif action == "route_hardship_forbearance":
            insert_at = max(len(updated.flow_stack) - 1, 0)
            updated.flow_stack.insert(insert_at, Frame(flow="promise_to_pay", step_index=0))
            slots["ptp_max_days"] = max(int(slots.get("ptp_max_days") or 14), 30)
        elif action == "route_hardship_review":
            slots["disposition"] = "FORBEARANCE_REVIEW"
            slots["transfer_to_human"] = True
        elif action == "set_hardship_disposition":
            if slots.get("disposition") != "FORBEARANCE_REVIEW":
                slots["disposition"] = "HARDSHIP_CAPTURED"
        elif action == "mark_vague_ptp":
            slots["vague_ptp"] = True
            slots["ptp_allowed"] = False
        elif action == "check_hardship_for_vague_ptp":
            reason = normalize_hardship_reason(slots.get("hardship_reason"))
            emotion = str(slots.get("emotion") or "")
            slots["hardship_context"] = bool(
                reason
                or slots.get("hardship_active")
                or emotion in {"stress", "fear", "hopelessness", "anxiety"}
            )
        elif action == "route_to_hardship":
            slots["_skip_flow_pop"] = True
            if updated.flow_stack:
                updated.flow_stack[-1] = Frame(flow="hardship", step_index=0)
            else:
                updated.flow_stack.append(Frame(flow="hardship", step_index=0))
        elif action == "push_specify_ptp":
            if updated.flow_stack:
                updated.flow_stack[-1] = Frame(flow="promise_to_pay", step_index=0)
            else:
                updated.flow_stack.append(Frame(flow="promise_to_pay", step_index=0))
            slots["_skip_flow_pop"] = True
        elif action == "route_vulnerable":
            slots["transfer_to_human"] = True
            slots["vulnerable_routed"] = True
        elif action == "evaluate_resume":
            slots["resume_parked_flow"] = any(
                frame.parked for frame in updated.flow_stack[:-1]
            )
        elif action == "drop_dispute_resume_parent":
            if len(updated.flow_stack) > 1:
                updated.flow_stack[-2].parked = False
            slots["dispute_dropped"] = True
        elif action == "drop_for_payment_found":
            slots["transfer_to_human"] = True
            slots["dispute_dropped"] = True
            slots["payment_found_handoff"] = True
        elif action == "set_identity_ok":
            slots["identity_ok"] = True
            slots["identity_verified"] = True
        elif action == "incr_identity_attempts":
            attempts = int(slots.get("identity_attempts") or 0) + 1
            slots["identity_attempts"] = attempts
            slots.pop("identity_response", None)
            slots["identity_verified"] = False
        elif action == "route_identity_failure":
            slots["transfer_to_human"] = True
            slots["identity_failed"] = True
            slots["end_call"] = True
        elif action == "apply_opt_out":
            flags = dict(slots.get("compliance_flags") or {})
            flags["opt_out"] = True
            channel = str(slots.get("opt_out_channel") or "all").lower().strip()
            if channel and channel != "all":
                flags["opt_out_channels"] = [channel]
            else:
                flags["opt_out_channels"] = ["voice", "sms", "whatsapp", "email"]
            slots["compliance_flags"] = flags
            slots["dunning_suppressed"] = True
            slots["disposition"] = "OPT_OUT"
            slots["opt_out_ack_this_turn"] = True
            slots["pressure_allowed"] = False
        elif action == "activate_third_party_mode":
            contact = normalize_third_party_contact(slots.get("third_party_contact_type"))
            slots["third_party_active"] = True
            slots["pressure_allowed"] = False
            if contact:
                slots["third_party_contact_type"] = contact
            if contact == "minor":
                slots["third_party_minor"] = True
            slots["borrower_name_hint"] = slots.get("borrower_name") or "the account holder"
        elif action == "classify_third_party_response":
            result = classify_third_party(slots.get("third_party_borrower_check"))
            if slots.get("third_party_contact_type") == "minor":
                result["third_party_minor"] = True
            slots.update(result)
        elif action == "confirm_not_borrower":
            flags = dict(slots.get("compliance_flags") or {})
            flags["wrong_number"] = True
            flags["number_suppressed"] = True
            flags["data_correction_needed"] = True
            slots["compliance_flags"] = flags
            slots["confirmed_not_borrower"] = True
            slots["dunning_suppressed"] = True
            slots["disposition"] = "WRONG_NUMBER"
            slots["pressure_allowed"] = False
        elif action == "route_minor_handoff":
            slots["third_party_minor"] = True
            slots["transfer_to_human"] = True
            slots["dunning_suppressed"] = True
            slots["disposition"] = "THIRD_PARTY"
            slots["minor_contact"] = True
            slots["pressure_allowed"] = False
        elif action == "halt_fraud_handoff":
            flags = dict(slots.get("compliance_flags") or {})
            flags["fraud_investigation"] = True
            flags["dispute_hold"] = True
            slots["compliance_flags"] = flags
            slots["fraud_claim_active"] = True
            slots["dunning_suppressed"] = True
            slots["transfer_to_human"] = True
            slots["disposition"] = "FRAUD_CLAIM"
            slots["pressure_allowed"] = False
        elif action == "halt_lawyer_handoff":
            flags = dict(slots.get("compliance_flags") or {})
            flags["legal_handoff"] = True
            slots["compliance_flags"] = flags
            slots["dunning_suppressed"] = True
            slots["transfer_to_human"] = True
            slots["disposition"] = "LAWYER_REP"
            slots["pressure_allowed"] = False
        elif action == "halt_deceased_handoff":
            flags = dict(slots.get("compliance_flags") or {})
            flags["vulnerable"] = True
            flags["dunning_suppressed"] = True
            flags["deceased_reported"] = True
            slots["compliance_flags"] = flags
            slots["deceased_reported"] = True
            slots["dunning_suppressed"] = True
            slots["transfer_to_human"] = True
            slots["tone_register"] = "care"
            slots["disposition"] = "DECEASED"
            slots["pressure_allowed"] = False
        elif action == "halt_incapacitated_handoff":
            flags = dict(slots.get("compliance_flags") or {})
            flags["vulnerable"] = True
            flags["dunning_suppressed"] = True
            slots["compliance_flags"] = flags
            slots["incapacitated_reported"] = True
            slots["dunning_suppressed"] = True
            slots["transfer_to_human"] = True
            slots["tone_register"] = "care"
            slots["disposition"] = "INCAPACITATED"
            slots["pressure_allowed"] = False
        elif action == "log_harassment_complaint":
            flags = dict(slots.get("compliance_flags") or {})
            flags["harassment_complaint"] = True
            slots["compliance_flags"] = flags
            slots["harassment_complaint_logged"] = True
            slots["dunning_suppressed"] = True
            slots["transfer_to_human"] = True
            slots["pressure_allowed"] = False
            slots["disposition"] = "HARASSMENT_COMPLAINT"
            slots["compliance_note_pending"] = {
                "type": "harassment_complaint",
                "text": "Borrower alleges harassment",
                "ts": datetime.now(UTC).isoformat(),
            }
        else:
            raise KeyError(f"Unknown local action: {action}")

        updated.slots = slots
        updated.events.append(
            Event(
                ts=datetime.now(UTC).isoformat(),
                kind="action",
                data={"action": action, "local": True},
            )
        )
        return updated


AsyncActionRunner = Callable[[str, ConversationState], Awaitable[ConversationState]]


def make_async_action_runner(tools: Any) -> AsyncActionRunner:
    registry = ActionRegistry(tools)
    active_marker: tuple[str, int] | None = None

    async def runner(action: str, state: ConversationState) -> ConversationState:
        nonlocal active_marker
        marker = (state.call_id, state.version)
        if active_marker != marker:
            registry.begin_turn(state.call_id, state.version)
            active_marker = marker
        return await registry.run_async(action, state)

    return runner


def make_action_runner(tools: Any) -> Callable[[str, ConversationState], ConversationState]:
    registry = ActionRegistry(tools)
    active_marker: tuple[str, int] | None = None

    def runner(action: str, state: ConversationState) -> ConversationState:
        nonlocal active_marker
        marker = (state.call_id, state.version)
        if active_marker != marker:
            registry.begin_turn(state.call_id, state.version)
            active_marker = marker
        return registry.run(action, state)

    return runner


async def run(action: str, state: ConversationState, tools: Any) -> ConversationState:
    """Async entry point for future /turn orchestration."""
    registry = ActionRegistry(tools)
    registry.begin_turn(state.call_id, state.version)
    return await registry.run_async(action, state)
