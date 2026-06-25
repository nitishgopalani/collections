import json
import logging
import re
from datetime import date
from typing import Any

from pydantic import ValidationError

from app.clients.llm_vertex import create_llm_client
from app.flows.loader import load_all_flows
from app.schemas.command import Command
from app.schemas.state import ConversationState

logger = logging.getLogger(__name__)

VALID_COMMANDS: frozenset[str] = frozenset(
    {
        "start_flow",
        "set_slot",
        "cancel_flow",
        "clarify",
        "human_handoff",
        "cannot_handle",
    }
)
ALLOWED_COMMAND_FIELDS: frozenset[str] = frozenset({"command", "flow", "name", "value", "reason"})
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_BASE_SLOT_NAMES: frozenset[str] = frozenset(
    {
        "amount_due",
        "dpd",
        "bucket",
        "call_date",
        "today",
        "ptp_date",
        "dispute_reason",
        "utr_reference",
    }
)


def known_slot_names() -> frozenset[str]:
    names = set(_BASE_SLOT_NAMES)
    for flow in load_all_flows().flows.values():
        for step in flow.steps:
            if step.collect:
                names.add(step.collect)
    return frozenset(names)


def build_system_prompt(today_iso: str) -> str:
    return (
        "You understand borrower utterances in a collections call. "
        "Output ONLY a JSON array of command objects. "
        "Allowed commands: start_flow, set_slot, cancel_flow, clarify, "
        "human_handoff, cannot_handle. "
        "Do NOT write reply text to the borrower. "
        "Do NOT decide policy or how hard to press. "
        "Resolve relative dates (kal, parso, next week) to ISO YYYY-MM-DD. "
        f"Today is {today_iso}. "
        "Use start_flow with a flow name from the candidate list. "
        "Use set_slot with name and value for extracted facts. "
        "Output multiple commands when the utterance has multiple signals."
    )


def _recent_turn_context(state: ConversationState, limit: int = 2) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for event in reversed(state.events):
        if event.kind in ("turn", "command", "turn_complete"):
            turns.append({"kind": event.kind, "data": event.data})
        if len(turns) >= limit:
            break
    return list(reversed(turns))


def build_user_prompt(
    transcript: str,
    candidate_flows: list[dict[str, Any]],
    state: ConversationState,
) -> str:
    payload = {
        "transcript": transcript,
        "candidate_flows": [
            {
                "name": flow.get("name"),
                "description": flow.get("description"),
                "score": flow.get("score"),
            }
            for flow in candidate_flows
        ],
        "slots": state.slots,
        "recent_turns": _recent_turn_context(state),
    }
    return json.dumps(payload, ensure_ascii=False)


def _candidate_flow_names(candidate_flows: list[dict[str, Any]]) -> frozenset[str]:
    names: set[str] = set()
    for flow in candidate_flows:
        name = flow.get("name")
        if name:
            names.add(str(name))
    return frozenset(names)


def parse_and_validate_commands(
    raw: str,
    *,
    candidate_flows: list[dict[str, Any]] | None = None,
) -> list[Command]:
    """Parse LLM JSON output; reject unknown commands/fields; malformed → clarify."""
    candidate_names = (
        _candidate_flow_names(candidate_flows) if candidate_flows is not None else None
    )
    allowed_slots = known_slot_names()

    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("command_gen: invalid JSON from LLM")
        return [Command(command="clarify")]

    if isinstance(data, dict):
        data = data.get("commands", data.get("command"))
    if not isinstance(data, list) or not data:
        return [Command(command="clarify")]

    validated: list[Command] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        command_type = item.get("command")
        if command_type not in VALID_COMMANDS:
            logger.warning("command_gen: rejected unknown command %s", command_type)
            continue

        cleaned = {key: item[key] for key in ALLOWED_COMMAND_FIELDS if key in item}
        if command_type == "start_flow":
            flow_name = cleaned.get("flow")
            if not flow_name or (
                candidate_names is not None and str(flow_name) not in candidate_names
            ):
                continue
        if command_type == "set_slot":
            slot_name = cleaned.get("name")
            if not slot_name or str(slot_name) not in allowed_slots:
                logger.warning("command_gen: rejected unknown slot %s", slot_name)
                continue
            if cleaned.get("value") is None:
                continue

        try:
            validated.append(Command.model_validate(cleaned))
        except ValidationError:
            logger.warning("command_gen: command failed validation: %s", cleaned)
            continue

    if not validated:
        return [Command(command="clarify")]
    return validated


def resolve_today(state: ConversationState) -> str:
    raw = state.slots.get("call_date") or state.slots.get("today")
    if isinstance(raw, str) and ISO_DATE_RE.match(raw[:10]):
        return raw[:10]
    return date.today().isoformat()


async def generate(
    text: str,
    state: ConversationState,
    candidate_flows: list[dict[str, Any]],
    *,
    llm: Any | None = None,
) -> list[Command]:
    today_iso = resolve_today(state)
    system = build_system_prompt(today_iso)
    user = build_user_prompt(text, candidate_flows, state)
    client = llm or create_llm_client()
    raw = await client.complete(system, user, json_only=True)
    return parse_and_validate_commands(raw, candidate_flows=candidate_flows)
