"""Flow simulator — drives handle_turn with scripted inputs and prints annotated traces."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.clients.tools_fixtures import BORROWER_FIXTURES
from app.clients.tools_sim import FakeToolClient
from app.engine.retrieval import clear_retrieval_cache
from app.engine.turn import handle_turn
from app.memory.audit import TurnAuditChain, query_turn_audits_by_borrower
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.schemas.state import BorrowerRecord, ConversationState
from app.sim.scripted_clients import ScriptedKB, ScriptedLLM

DELIBERATE_EMPTY_GATE_REASONS: frozenset[str] = frozenset(
    {
        "outside_call_window",
        "opt_out_active",
        "attempt_cap_daily",
    }
)


@dataclass
class SimTurnSpec:
    transcript: str = ""
    label: str = ""
    opener: bool = False
    turn_meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnTraceRecord:
    turn_index: int
    label: str
    borrower_text: str
    active_flow: str
    reply_id: str | None
    variant_index: int | None
    reply_text: str
    gate_verdict: str
    gate_reason: str
    slots_set: dict[str, Any]
    actions_executed: list[str]
    commands: list[dict[str, Any]]
    ok: bool
    issue: str | None = None
    end_call: bool = False


@dataclass
class SimResult:
    name: str
    call_id: str
    borrower_id: str
    gate_now: datetime | None
    traces: list[TurnTraceRecord]
    all_ok: bool
    issues: list[str]


def parse_gate_now(raw: str | None, *, timezone: str = "Asia/Kolkata") -> datetime | None:
    if not raw:
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Invalid gate_now timestamp: {raw!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
    return parsed


@contextmanager
def gate_clock_override(gate_now: datetime | None):
    """Pin compliance gate clock without changing CALL_WINDOW env."""
    if gate_now is None:
        yield
        return

    def _fixed_clock(state: ConversationState, tenant_cfg: Any) -> datetime:
        _ = state, tenant_cfg
        return gate_now

    with patch("app.engine.turn.gate_clock_from_state", side_effect=_fixed_clock):
        yield


def active_flow_label(state: ConversationState | None) -> str:
    if state is None or not state.flow_stack:
        return "(none)"
    top = state.flow_stack[-1]
    parked = " [parked]" if top.parked else ""
    return f"{top.flow}@{top.step_index}{parked}"


def slots_from_commands(commands: list[dict[str, Any]]) -> dict[str, Any]:
    slots: dict[str, Any] = {}
    for cmd in commands:
        if cmd.get("command") == "set_slot" and cmd.get("name"):
            slots[str(cmd["name"])] = cmd.get("value")
    return slots


def evaluate_turn(reply_text: str, gate_verdict: str, gate_reason: str) -> tuple[bool, str | None]:
    if reply_text.strip():
        return True, None
    if gate_verdict == "block" and gate_reason in DELIBERATE_EMPTY_GATE_REASONS:
        return True, None
    if not gate_reason or gate_reason == "ok":
        return False, "empty reply_text with no deliberate gate reason"
    if gate_verdict in {"block", "modify"} and gate_reason:
        return True, None
    return False, f"empty reply_text (gate={gate_verdict}, reason={gate_reason or 'none'})"


def format_gate_line(verdict: str, reason: str, reply_text: str) -> str:
    if verdict == "allow":
        return f"allow / {reason or 'ok'}"
    if verdict == "modify":
        return f"fallback / {reason or 'modified'}"
    if not reply_text.strip() and reason in DELIBERATE_EMPTY_GATE_REASONS:
        return f"silent / {reason}"
    if verdict == "block":
        return f"block / {reason or 'blocked'}"
    return f"{verdict or 'unknown'} / {reason or 'none'}"


def borrower_from_spec(borrower_id: str, spec: dict[str, Any]) -> BorrowerRecord:
    fixture_id = spec.get("borrower_fixture")
    loan: dict[str, Any] = {}
    identity: dict[str, Any] = {}
    compliance_flags: dict[str, Any] = {}

    if fixture_id:
        fixture = BORROWER_FIXTURES.get(fixture_id)
        if fixture is None:
            raise ValueError(f"Unknown borrower_fixture: {fixture_id!r}")
        loan = {
            key: fixture[key]
            for key in (
                "amount_due",
                "dpd",
                "bucket",
                "principal",
                "interest",
                "charges",
                "due_date",
                "loan_tenure_months",
                "interest_rate_pct",
            )
            if key in fixture
        }
        if "identity" in fixture:
            identity = dict(fixture["identity"])
        if fixture.get("vulnerable"):
            compliance_flags["vulnerable"] = True

    override = spec.get("borrower") or {}
    if "loan" in override:
        loan.update(override["loan"])
    if "identity" in override:
        identity.update(override["identity"])
    if "compliance_flags" in override:
        compliance_flags.update(override["compliance_flags"])

    return BorrowerRecord(
        borrower_id=borrower_id,
        loan=loan,
        identity=identity,
        compliance_flags=compliance_flags,
    )


def load_sim_script(path: str | Path) -> dict[str, Any]:
    script_path = Path(path)
    with script_path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Sim script must be a JSON object: {script_path}")
    return data


async def simulate_conversation(
    *,
    name: str,
    call_id: str,
    tenant_id: str,
    borrower_id: str,
    turns: list[SimTurnSpec],
    kb_results: list[dict[str, Any]] | None = None,
    llm_commands: list[list[dict[str, Any]]] | None = None,
    borrower_spec: dict[str, Any] | None = None,
    borrower_context: dict[str, Any] | None = None,
    agent_id: str | None = None,
    locale: str = "hi-IN",
    call_date: str | None = None,
    gate_now: datetime | None = None,
    use_live_llm: bool = False,
    default_turn_meta: dict[str, Any] | None = None,
) -> SimResult:
    """Run a scripted conversation through handle_turn and collect annotated traces."""
    clear_retrieval_cache()
    memory = InMemoryMemoryStore()
    kb = ScriptedKB(list(kb_results or []))
    llm = ScriptedLLM(list(llm_commands or [])) if not use_live_llm else None
    if use_live_llm:
        from app.clients.llm_vertex import create_llm_client

        llm = create_llm_client()
    tools = FakeToolClient()
    tools.reset()

    borrower = borrower_from_spec(borrower_id, borrower_spec or {})
    if borrower_context:
        from app.ws.borrower_context import apply_borrower_context_to_record

        borrower = apply_borrower_context_to_record(borrower, borrower_context)
    await memory.save_borrower(borrower)

    traces: list[TurnTraceRecord] = []
    issues: list[str] = []
    base_turn_meta = dict(default_turn_meta or {})
    if borrower_context:
        base_turn_meta.setdefault("borrower_context", dict(borrower_context))

    with gate_clock_override(gate_now):
        for index, turn in enumerate(turns, start=1):
            turn_meta = dict(base_turn_meta)
            turn_meta.update(turn.turn_meta)
            if call_date:
                turn_meta.setdefault("call_date", call_date)
            if turn.opener:
                turn_meta["opener"] = True

            request = TurnRequest(
                call_id=call_id,
                tenant_id=tenant_id,
                borrower_id=borrower_id,
                transcript=turn.transcript,
                locale=locale,
                turn_meta=turn_meta,
                agent_id=agent_id,
            )

            before_state = await memory.load_state(call_id)
            before_slots = dict(before_state.slots) if before_state else {}

            response = await handle_turn(
                request,
                memory=memory,
                kb=kb,
                llm=llm,
                tools=tools,
            )

            after_state = await memory.load_state(call_id)
            audits = await query_turn_audits_by_borrower(memory, borrower_id)
            chain: TurnAuditChain | None = audits[-1] if audits else None

            gate_verdict = chain.gate_verdict if chain else ""
            gate_reason = chain.gate_reason if chain else ""
            commands = chain.commands if chain else []
            slot_delta = {
                key: value
                for key, value in (after_state.slots if after_state else {}).items()
                if before_slots.get(key) != value
            }
            slots_set = slots_from_commands(commands) or slot_delta

            ok, issue = evaluate_turn(response.reply_text, gate_verdict, gate_reason)
            if not ok and issue:
                issues.append(f"turn {index}: {issue}")

            traces.append(
                TurnTraceRecord(
                    turn_index=index,
                    label=turn.label or f"turn-{index}",
                    borrower_text=turn.transcript,
                    active_flow=active_flow_label(after_state),
                    reply_id=response.reply_id or (chain.reply_id if chain else None),
                    variant_index=response.variant_index
                    if response.variant_index is not None
                    else (chain.variant_index if chain else None),
                    reply_text=response.reply_text,
                    gate_verdict=gate_verdict,
                    gate_reason=gate_reason,
                    slots_set=slots_set,
                    actions_executed=list(response.actions_executed),
                    commands=list(commands),
                    end_call=response.end_call,
                    ok=ok,
                    issue=issue,
                )
            )

    clear_retrieval_cache()
    return SimResult(
        name=name,
        call_id=call_id,
        borrower_id=borrower_id,
        gate_now=gate_now,
        traces=traces,
        all_ok=not issues,
        issues=issues,
    )


def _turn_specs_from_script(script: dict[str, Any]) -> list[SimTurnSpec]:
    raw_turns = script.get("turns")
    if not isinstance(raw_turns, list) or not raw_turns:
        raise ValueError("Sim script must include a non-empty 'turns' list")
    specs: list[SimTurnSpec] = []
    for item in raw_turns:
        if isinstance(item, str):
            specs.append(SimTurnSpec(transcript=item))
            continue
        if not isinstance(item, dict):
            raise ValueError("Each turn must be a string or object")
        specs.append(
            SimTurnSpec(
                transcript=str(item.get("transcript", "")),
                label=str(item.get("label", "")),
                opener=bool(item.get("opener", False)),
                turn_meta=dict(item.get("turn_meta") or {}),
            )
        )
    return specs


async def run_sim_script(
    script: dict[str, Any],
    *,
    gate_now_override: datetime | None = None,
    use_live_llm: bool = False,
) -> SimResult:
    gate_now = gate_now_override or parse_gate_now(script.get("gate_now"))
    return await simulate_conversation(
        name=str(script.get("name") or script.get("description") or "sim"),
        call_id=str(script["call_id"]),
        tenant_id=str(script.get("tenant_id", "default")),
        borrower_id=str(script["borrower_id"]),
        turns=_turn_specs_from_script(script),
        kb_results=list(script.get("kb") or []),
        llm_commands=list(script.get("llm_commands") or []),
        borrower_spec={
            "borrower_fixture": script.get("borrower_fixture"),
            "borrower": script.get("borrower"),
        },
        agent_id=script.get("agent_id"),
        locale=str(script.get("locale", "hi-IN")),
        call_date=script.get("call_date"),
        gate_now=gate_now,
        use_live_llm=use_live_llm,
        default_turn_meta=dict(script.get("turn_meta") or {}),
        borrower_context=dict(script.get("borrower_context") or {}),
    )


def format_sim_transcript(result: SimResult) -> str:
    lines: list[str] = []
    lines.append(f"=== Flow sim: {result.name} ===")
    lines.append(f"call_id={result.call_id} borrower_id={result.borrower_id}")
    if result.gate_now is not None:
        lines.append(f"gate_now={result.gate_now.isoformat()}")
    lines.append("")

    for trace in result.traces:
        lines.append(f"--- Turn {trace.turn_index} [{trace.label}] ---")
        borrower_line = trace.borrower_text if trace.borrower_text else "(opener / empty transcript)"
        lines.append(f"Borrower: {borrower_line}")
        lines.append(f"Active flow: {trace.active_flow}")
        reply_id = trace.reply_id or "(none)"
        variant = trace.variant_index if trace.variant_index is not None else "-"
        lines.append(f"Reply: {reply_id} (variant {variant})")
        text = trace.reply_text if trace.reply_text else "(empty)"
        lines.append(f"Text: {text}")
        lines.append(
            f"Gate: {format_gate_line(trace.gate_verdict, trace.gate_reason, trace.reply_text)}"
        )
        if trace.slots_set:
            slot_bits = ", ".join(f"{key}={value!r}" for key, value in trace.slots_set.items())
            lines.append(f"Slots set: {slot_bits}")
        if trace.actions_executed:
            lines.append(f"Actions: {', '.join(trace.actions_executed)}")
        if trace.end_call:
            lines.append("End call: true")
        if trace.issue:
            lines.append(f"ISSUE: {trace.issue}")
        lines.append("")

    ok_count = sum(1 for trace in result.traces if trace.ok)
    total = len(result.traces)
    lines.append("=== Summary ===")
    lines.append(
        f"Speakable or deliberate silent: {ok_count}/{total} turns"
        + (" — ALL OK" if result.all_ok else " — ISSUES FOUND")
    )
    for issue in result.issues:
        lines.append(f"  - {issue}")
    return "\n".join(lines)
