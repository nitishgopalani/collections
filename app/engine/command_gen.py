import json
import logging
import re
from dataclasses import dataclass, field
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

# Hydrated/context slots the LLM must not set via set_slot (not flow collect steps).
READ_ONLY_LLM_SLOTS: frozenset[str] = frozenset(
    {
        "borrower_name",
        "borrower_phone",
        "phone",
        "amount_due",
        "dpd",
        "bucket",
        "account_ref",
        "language",
        "compliance_flags",
        "trust",
        "risk_flags",
        "persona",
        "emotion",
        "emotion_intensity",
        "tone_register",
        "recovery",
        "identity_ok",
        "last_question_slot",
        "_force_test_flow",
    }
)

_BASE_SLOT_NAMES: frozenset[str] = frozenset(
    {
        "amount_due",
        "dpd",
        "bucket",
        "call_date",
        "today",
        "ptp_date",
        "dispute_reason",
        "dispute_type",
        "dispute_claim",
        "utr_reference",
        "hardship_reason",
        "hardship_path",
        "hardship_expected_duration",
        "third_party_contact_type",
        "third_party_borrower_check",
        "due_date",
        "loan_tenure_months",
        "interest_rate_pct",
        "critical_confirm_label",
        "callback_window",
        "prior_call_context",
        "negotiation_request",
    }
)


def known_slot_names() -> frozenset[str]:
    names = set(_BASE_SLOT_NAMES)
    for flow in load_all_flows().flows.values():
        for step in flow.steps:
            if step.collect:
                names.add(step.collect)
    return frozenset(names)


def known_flow_names() -> frozenset[str]:
    return frozenset(load_all_flows().flows.keys())


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


def slots_for_llm_prompt(slots: dict[str, Any]) -> dict[str, Any]:
    """Expose only settable / operational slots — hide hydrated read-only context."""
    return {
        key: value
        for key, value in slots.items()
        if key not in READ_ONLY_LLM_SLOTS
        and not str(key).startswith("_")
        and value is not None
    }


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
        "slots": slots_for_llm_prompt(state.slots),
        "recent_turns": _recent_turn_context(state),
        "active_flow_slot_hints": _active_flow_slot_hints(state),
    }
    return json.dumps(payload, ensure_ascii=False)


def _active_flow_slot_hints(state: ConversationState) -> list[dict[str, Any]]:
    """Tell the LLM which set_slot names/values the active collect step expects."""
    if not state.flow_stack:
        return []
    frame = state.flow_stack[-1]
    flows = load_all_flows()
    flow = flows.flows.get(frame.flow)
    if flow is None or frame.step_index >= len(flow.steps):
        return []
    step = flow.steps[frame.step_index]
    if not step.collect:
        return []

    hints: dict[str, dict[str, Any]] = {
        "identity_response": {
            "slot": "identity_response",
            "note": (
                "Borrower's reply to the right-party name check. Set to 'haan' when "
                "they confirm/affirm in any form, 'nahi' when they deny, or their "
                "spoken name verbatim if they state it — never set borrower_name."
            ),
            "map_examples": {
                "haan/ji/ji haan/bilkul/sahi/main bol raha hoon/yes/speaking": "haan",
                "nahi/galat number/wrong person/aap galat number par hain": "nahi",
                "main Rajesh hoon": "Rajesh",
            },
        },
        "identity_confirmed": {
            "slot": "identity_confirmed",
            "values": ["confirmed", "denied"],
            "map_examples": {
                "haan/ji/yes/bol raha": "confirmed",
                "nahi/galat number/wrong": "denied",
            },
        },
        "payment_intent": {
            "slot": "payment_intent",
            "values": ["willing", "dispute"],
            "map_examples": {
                "payment kar doonga/kar dunga/haan": "willing",
                "issue/dispute/problem": "dispute",
            },
        },
        "payment_ack": {
            "slot": "payment_ack",
            "values": ["paid"],
            "map_examples": {
                "kar diya/ho gaya/done/paid": "paid",
            },
        },
        "test_identity_intent": {
            "slot": "test_identity_intent",
            "values": ["confirmed", "denied"],
        },
        "test_payment_intent": {
            "slot": "test_payment_intent",
            "values": ["willing", "dispute"],
        },
        "test_paid_intent": {
            "slot": "test_paid_intent",
            "values": ["paid"],
        },
        # ---- Salary On Time (pre-closure) collect slots ----
        "sot_identity_response": {
            "slot": "sot_identity_response",
            "values": ["confirmed", "relation", "denied"],
            "map_examples": {
                "haan/ji/main bol raha hoon/main hi hoon/yes/speaking": "confirmed",
                "main inka pati/patni/bhai/beta/relation batao": "relation",
                "nahi/galat number/wrong person": "denied",
            },
        },
        "sot_knows_customer": {
            "slot": "sot_knows_customer",
            "values": ["true", "false"],
            "map_examples": {"haan jaanta hoon/yes": "true", "nahi jaanta/no": "false"},
        },
        "sot_relation_type": {
            "slot": "sot_relation_type",
            "note": "Caller's relation to the customer, verbatim (pati, patni, bhai, behen, pita, maa, cousin, dost).",
        },
        "sot_sibling_type": {
            "slot": "sot_sibling_type",
            "values": ["real", "cousin"],
            "map_examples": {"sagaa bhai/real brother/behen": "real", "cousin": "cousin"},
        },
        "sot_restricted_followup": {
            "slot": "sot_restricted_followup",
            "values": ["wants_details", "alternate_number", "unavailable"],
            "map_examples": {
                "kitna paisa/detail batao/loan kya hai": "wants_details",
                "doosra number/alternate number par": "alternate_number",
                "abhi available nahi/baad mein": "unavailable",
            },
        },
        "sot_payment_intent": {
            "slot": "sot_payment_intent",
            "values": ["willing", "already_paid", "refused"],
            "map_examples": {
                "haan aaj kar dunga/karunga/payment kar dunga/theek hai": "willing",
                "pay kar diya/already paid/ho gaya hai": "already_paid",
                "nahi/abhi nahi/paisa nahi hai/baad mein": "refused",
            },
        },
        "sot_payment_problem": {
            "slot": "sot_payment_problem",
            "note": (
                "Borrower's response after being asked the reason. ALWAYS set this to "
                "their words (verbatim/short) so the conversation advances — whether it "
                "is a reason they can't pay OR a change of mind (e.g. they now agree to pay)."
            ),
        },
        "sot_payment_intent_2": {
            "slot": "sot_payment_intent_2",
            "values": ["willing", "refused"],
            "map_examples": {
                "haan aaj kar dunga/theek hai karunga": "willing",
                "nahi/abhi nahi ho payega": "refused",
            },
        },
        "sot_commit_timing": {
            "slot": "sot_commit_timing",
            "values": ["today", "tomorrow", "before_due", "on_due", "after_due"],
            "map_examples": {
                "aaj/abhi/aaj hi/jaldi hi": "today",
                "kal": "tomorrow",
                "due date se pehle/is hafte": "before_due",
                "due date ko/last date ko": "on_due",
                "due date ke baad/agle mahine/2-3 din baad": "after_due",
            },
        },
        "sot_customer_time": {
            "slot": "sot_customer_time",
            "note": "Time of day borrower will pay, verbatim (e.g. 'shaam 5 baje', 'dopahar', 'raat tak').",
        },
        "sot_ondue_decision": {
            "slot": "sot_ondue_decision",
            "values": ["pay_today", "later"],
            "map_examples": {"haan aaj kar dunga": "pay_today", "nahi due date ko": "later"},
        },
        "sot_afterdue_decision": {
            "slot": "sot_afterdue_decision",
            "values": ["pay_today", "later"],
            "map_examples": {"haan aaj kar dunga": "pay_today", "nahi baad mein": "later"},
        },
        "sot_final_confirm": {
            "slot": "sot_final_confirm",
            "values": ["yes", "no"],
            "map_examples": {"haan/confirm/theek hai/pakka": "yes", "nahi/change karna hai": "no"},
        },
    }
    hint = hints.get(step.collect)
    if hint is None:
        return [{"slot": step.collect, "note": "set_slot with an appropriate value"}]
    return [hint]


@dataclass
class CommandParseResult:
    commands: list[Command] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)
    raw: str = ""


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
) -> CommandParseResult:
    """Parse LLM JSON output; reject unknown commands/fields; malformed → clarify."""
    _ = candidate_flows
    allowed_slots = known_slot_names()
    rejections: list[str] = []

    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError:
        logger.info("command_gen: invalid JSON from LLM raw=%s", (raw or "")[:300])
        return CommandParseResult(commands=[Command(command="clarify")], raw=raw)

    if isinstance(data, dict):
        data = data.get("commands", data.get("command"))
    if not isinstance(data, list) or not data:
        logger.info("command_gen: no commands parsed raw=%s", (raw or "")[:300])
        return CommandParseResult(commands=[Command(command="clarify")], raw=raw)

    validated: list[Command] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        command_type = item.get("command")
        if command_type not in VALID_COMMANDS:
            reason = f"rejected unknown command {command_type}"
            rejections.append(reason)
            logger.info("command_gen: %s", reason)
            continue

        cleaned = {key: item[key] for key in ALLOWED_COMMAND_FIELDS if key in item}
        if command_type == "start_flow":
            flow_name = cleaned.get("flow")
            if not flow_name or str(flow_name) not in known_flow_names():
                reason = f"rejected unknown flow {flow_name}"
                rejections.append(reason)
                logger.info("command_gen: %s", reason)
                continue
        if command_type == "set_slot":
            slot_name = cleaned.get("name")
            if not slot_name or str(slot_name) not in allowed_slots:
                reason = f"rejected unknown slot {slot_name}"
                rejections.append(reason)
                logger.info("command_gen: %s", reason)
                continue
            if cleaned.get("value") is None:
                reason = f"rejected empty slot {slot_name}"
                rejections.append(reason)
                logger.info("command_gen: %s", reason)
                continue

        try:
            validated.append(Command.model_validate(cleaned))
        except ValidationError:
            reason = f"rejected invalid command {cleaned}"
            rejections.append(reason)
            logger.info("command_gen: %s", reason)
            continue

    if not validated:
        return CommandParseResult(
            commands=[Command(command="clarify")], rejections=rejections, raw=raw
        )
    return CommandParseResult(commands=validated, rejections=rejections, raw=raw)


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
) -> CommandParseResult:
    today_iso = resolve_today(state)
    system = build_system_prompt(today_iso)
    user = build_user_prompt(text, candidate_flows, state)
    client = llm or create_llm_client()
    raw = await client.complete(system, user, json_only=True)
    return parse_and_validate_commands(raw, candidate_flows=candidate_flows)
