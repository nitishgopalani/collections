import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pydantic import ValidationError

from app.clients.llm_vertex import create_llm_client
from app.flows.loader import get_flow_set
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
        "respond",
    }
)
ALLOWED_COMMAND_FIELDS: frozenset[str] = frozenset(
    {"command", "flow", "name", "value", "reason", "text"}
)
# Single source for respond length cap (D-2 confirmation pending).
RESPOND_MAX_CHARS: int = 220
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Hydrated facts exposed under ``facts`` when respond is enabled (still read-only).
FACT_SLOTS_FOR_RESPOND: frozenset[str] = frozenset(
    {
        "repay_amount",
        "due_date",
        "offer_amount",
        "discount_amount",
        "loan_amount",
        "disbursal_date",
        "customer_name",
        "borrower_name",
        "amount_due",
        # Payment-history facts (when hydrated via tools / borrower record).
        "amount_paid",
        "last_payment_amount",
        "last_payment_date",
        # PaisaLo facts (P5).
        "days_past_due",
        "branch",
        "branch_address",
        "last_date_paid",
        "product",
        "npa_flag",
    }
)

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
        # Borrower/account facts the LLM must never rewrite mid-call.
        "due_date",
        "repay_amount",
        "offer_amount",
        "discount_amount",
        "loan_amount",
        "disbursal_date",
        "amount_paid",
        "last_payment_amount",
        "last_payment_date",
        "days_past_due",
        "branch",
        "branch_address",
        "last_date_paid",
        "product",
        "npa_flag",
        "voice_id",
        "plo_scenario",
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
    for flow in get_flow_set().flows.values():
        for step in flow.steps:
            if step.collect:
                names.add(step.collect)
    return frozenset(names)


def known_flow_names() -> frozenset[str]:
    return frozenset(get_flow_set().flows.keys())


def _clean_respond_text(text: str) -> str:
    """Strip newlines/markdown; collapse whitespace for TTS-safe respond text."""
    cleaned = (text or "").replace("\n", " ").replace("\r", " ")
    cleaned = re.sub(r"[*_`#]+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def build_system_prompt(
    today_iso: str,
    blocked_commands: frozenset[str] = frozenset(),
    *,
    respond_enabled: bool = False,
    unknown_info_reply: str = "",
    catalog_mode: bool = False,
) -> str:
    command_vocab = (
        "start_flow",
        "set_slot",
        "cancel_flow",
        "clarify",
        "human_handoff",
        "cannot_handle",
        "respond",
    )
    allowed = [
        c
        for c in command_vocab
        if c not in blocked_commands and (c != "respond" or respond_enabled)
    ]
    parts = [
        "You understand borrower utterances in a collections call. ",
        "Output ONLY a JSON array of command objects. ",
        f"Allowed commands: {', '.join(allowed)}. ",
    ]
    if respond_enabled:
        unknown = (unknown_info_reply or "").strip() or "I don't have that information."
        parts.append(
            "Do NOT write free-form borrower replies except via respond.text. "
        )
        if catalog_mode:
            parts.append(
                "If the borrower asks a question no flow covers, output "
                "{\"command\":\"respond\",\"text\":\"<ONE short Devanagari sentence "
                "answering ONLY from the facts in slots/facts>\"}. "
                "NEVER invent amounts, dates, waivers, penalties, or policies. "
                "When quoting an amount from facts, write the digit form as "
                "'<N> rupaye' (e.g. '2300 rupaye') — not Hindi word-numbers — "
                "so fact-grounding can verify the value. "
                "Use amount_paid/last_payment_* only when present in facts; "
                "do not answer 'kitni payment di / already paid' from repay_amount. "
                f"If the answer is not in facts/slots, respond with this unknown-info "
                f"line verbatim: {json.dumps(unknown, ensure_ascii=False)}. "
            )
    else:
        parts.append("Do NOT write reply text to the borrower. ")
    parts.extend(
        [
            "Do NOT decide policy or how hard to press. ",
            "Resolve relative dates (kal, parso, next week) to ISO YYYY-MM-DD. ",
            f"Today is {today_iso}. ",
            "Use start_flow with a flow name from the candidate list. ",
            "Use set_slot with name and value for extracted facts. ",
            "Output multiple commands when the utterance has multiple signals.",
        ]
    )
    return "".join(parts)


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


def facts_for_respond_prompt(slots: dict[str, Any]) -> dict[str, Any]:
    """Read-only account facts the LLM may quote in respond.text."""
    return {
        key: slots[key]
        for key in sorted(FACT_SLOTS_FOR_RESPOND)
        if slots.get(key) is not None
    }


def build_user_prompt(
    transcript: str,
    candidate_flows: list[dict[str, Any]],
    state: ConversationState,
    *,
    catalog_mode: bool = False,
    respond_enabled: bool = False,
) -> str:
    if catalog_mode:
        flow_rows = [
            {
                "name": flow.get("name"),
                "description": flow.get("description"),
            }
            for flow in candidate_flows
        ]
    else:
        flow_rows = [
            {
                "name": flow.get("name"),
                "description": flow.get("description"),
                "score": flow.get("score"),
            }
            for flow in candidate_flows
        ]
    payload: dict[str, Any] = {
        "transcript": transcript,
        "candidate_flows": flow_rows,
        "slots": slots_for_llm_prompt(state.slots),
        "recent_turns": _recent_turn_context(state),
        "active_flow_slot_hints": _active_flow_slot_hints(state),
    }
    if respond_enabled:
        payload["facts"] = facts_for_respond_prompt(state.slots)
    if catalog_mode:
        payload["routing_note"] = (
            "candidate_flows is the COMPLETE catalog. Prefer set_slot for the "
            "awaited slot; start_flow ONLY when the borrower clearly raises that topic."
        )
    return json.dumps(payload, ensure_ascii=False)


def _active_flow_slot_hints(state: ConversationState) -> list[dict[str, Any]]:
    """Tell the LLM which set_slot names/values the active collect step expects."""
    if not state.flow_stack:
        return []
    frame = state.flow_stack[-1]
    flows = get_flow_set()
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
            "note": (
                "Answer to 'will you pay TODAY / by when will you pay'. 'willing' = will "
                "pay today (incl. a time today like 'aaj sham tak'). Any later day "
                "('kal', 'parso', a future date, 'baad mein') or a plain no = 'refused' "
                "so we move to the push step."
            ),
            "map_examples": {
                "haan aaj kar dunga/karunga/payment kar dunga/theek hai/aaj sham tak": "willing",
                "pay kar diya/already paid/ho gaya hai": "already_paid",
                "nahi/abhi nahi/paisa nahi hai/baad mein/kal/parso/agle mahine": "refused",
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
        # On-Due / Post-Due push ladders re-ask the same yes/no across successive
        # pushes; each push uses its own intent slot so the executor doesn't skip a
        # collect whose slot is already filled from an earlier push.
        "sot_payment_intent_3": {
            "slot": "sot_payment_intent_3",
            "values": ["willing", "refused"],
            "map_examples": {
                "haan aaj kar dunga/theek hai karunga": "willing",
                "nahi/abhi nahi ho payega/kal": "refused",
            },
        },
        "sot_payment_intent_4": {
            "slot": "sot_payment_intent_4",
            "values": ["willing", "refused"],
            "map_examples": {
                "haan aaj kar dunga/theek hai karunga": "willing",
                "nahi/abhi nahi ho payega/kal": "refused",
            },
        },
        "sot_payment_intent_5": {
            "slot": "sot_payment_intent_5",
            "values": ["willing", "refused"],
            "map_examples": {
                "haan aaj kar dunga/theek hai karunga": "willing",
                "nahi/abhi nahi ho payega/kal": "refused",
            },
        },
        "sot_commit_timing": {
            "slot": "sot_commit_timing",
            "values": ["today", "tomorrow", "before_due", "on_due", "after_due"],
            "note": (
                "ALWAYS output one of the enum words below — NEVER an ISO date here. "
                "When already in the commitment step, ANY 'pay today' answer — including a specific "
                "time today (aaj sham 6 baje, aaj raat tak) — is a commitment: set 'today'. Do NOT "
                "start an objection flow for a pay-today commitment. 'kal' = tomorrow, "
                "'parso/parson' (day after) and '2-3 din baad' = after_due."
            ),
            "map_examples": {
                "aaj/abhi/aaj hi/jaldi hi/aaj sham/sham ko/sham 6 baje/aaj raat/today evening": "today",
                "kal/kal kar dunga/kal tak": "tomorrow",
                "due date se pehle/is hafte": "before_due",
                "due date ko/last date ko": "on_due",
                "parso/parson/due date ke baad/agle mahine/2-3 din baad": "after_due",
            },
        },
        "sot_customer_time": {
            "slot": "sot_customer_time",
            "note": (
                "Time of day the borrower will pay, captured VERBATIM as a time phrase "
                "(e.g. 'shaam 5 baje', 'dopahar', 'raat tak', '6 baje'). This is a TIME, "
                "not a date — NEVER output an ISO date or datetime here. Ignore the "
                "global 'resolve dates to ISO' rule for this slot."
            ),
        },
        "sot_ondue_decision": {
            "slot": "sot_ondue_decision",
            "values": ["pay_today", "later"],
            "note": (
                "We asked whether they will pay TODAY or on/after the due date. ALWAYS "
                "answer with this slot — NEVER sot_commit_timing here. 'aaj/abhi' = "
                "pay_today; ANY later day ('kal', 'parso/parson', 'due date ko', "
                "'2-3 din baad') = later. Do not start an objection flow."
            ),
            "map_examples": {
                "haan aaj kar dunga/aaj hi/abhi": "pay_today",
                "nahi due date ko/kal/parso/parson/baad mein/2-3 din baad": "later",
            },
        },
        "sot_afterdue_decision": {
            "slot": "sot_afterdue_decision",
            "values": ["pay_today", "later"],
            "map_examples": {"haan aaj kar dunga": "pay_today", "nahi baad mein": "later"},
        },
        "sot_final_confirm": {
            "slot": "sot_final_confirm",
            "values": ["yes", "no"],
            "note": (
                "Final confirmation of the already-captured payment time. The borrower "
                "RE-STATING the same time (e.g. 'sham mein ho jayegi', 'haan kal kar dunga') "
                "IS a confirmation: set 'yes'. Only set 'no' if they want to CHANGE the "
                "date/time. Do NOT re-set sot_customer_time here."
            ),
            "map_examples": {
                "haan/confirm/theek hai/pakka/ho jayegi/kar dunga/sham mein ho jayegi": "yes",
                "nahi/change karna hai/doosra time": "no",
            },
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
    blocked_commands: frozenset[str] = frozenset(),
    catalog_mode: bool = False,
    respond_enabled: bool = False,
) -> CommandParseResult:
    """Parse LLM JSON output; reject unknown/blocked commands/fields; malformed → clarify."""
    allowed_slots = known_slot_names()
    # Tier-2 catalog mode: start_flow must be in the offered catalog (enforces
    # deflection filtering even when the LLM client ignores response_schema).
    # Legacy digression/RAG keeps the prior known_flow_names-only check.
    catalog_names = _candidate_flow_names(candidate_flows or [])
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
        if command_type in blocked_commands:
            # Tenant does not support this command (e.g. salary_on_time has no live
            # human queue) — drop it so the LLM can't stall the flow with it.
            reason = f"rejected blocked command {command_type}"
            rejections.append(reason)
            logger.info("command_gen: %s", reason)
            continue

        cleaned = {key: item[key] for key in ALLOWED_COMMAND_FIELDS if key in item}
        # Key-alias tolerance: some providers (e.g. Groq best-effort JSON) emit the
        # right value under the wrong key. Recover those instead of discarding a
        # correct answer. (Strict structured output prevents this, but this is the
        # belt-and-suspenders fallback for non-strict models.)
        if "name" not in cleaned and item.get("slot") is not None:
            cleaned["name"] = item["slot"]
        if command_type == "start_flow" and not cleaned.get("flow") and cleaned.get("name"):
            cleaned["flow"] = cleaned.pop("name")
        if command_type == "start_flow":
            flow_name = cleaned.get("flow")
            if not flow_name or str(flow_name) not in known_flow_names():
                reason = f"rejected unknown flow {flow_name}"
                rejections.append(reason)
                logger.info("command_gen: %s", reason)
                continue
            if (
                catalog_mode
                and catalog_names
                and str(flow_name) not in catalog_names
            ):
                reason = f"rejected out-of-catalog flow {flow_name}"
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
        if command_type == "respond":
            if not respond_enabled:
                reason = "rejected respond (respond_enabled=false)"
                rejections.append(reason)
                logger.info("command_gen: %s", reason)
                continue
            text = _clean_respond_text(str(cleaned.get("text") or ""))
            if not text:
                reason = "rejected empty respond text"
                rejections.append(reason)
                logger.info("command_gen: %s", reason)
                continue
            if len(text) > RESPOND_MAX_CHARS:
                reason = f"rejected respond text over {RESPOND_MAX_CHARS} chars"
                rejections.append(reason)
                logger.info("command_gen: %s", reason)
                continue
            cleaned["text"] = text

        try:
            validated.append(Command.model_validate(cleaned))
        except ValidationError:
            reason = f"rejected invalid command {cleaned}"
            rejections.append(reason)
            logger.info("command_gen: %s", reason)
            continue

    # respond may co-exist with set_slot; never with start_flow (drop respond).
    if any(c.command == "start_flow" for c in validated) and any(
        c.command == "respond" for c in validated
    ):
        validated = [c for c in validated if c.command != "respond"]
        rejections.append("dropped respond co-occurring with start_flow")
        logger.info("command_gen: dropped respond co-occurring with start_flow")

    if not validated:
        return CommandParseResult(
            commands=[Command(command="clarify")], rejections=rejections, raw=raw
        )
    return CommandParseResult(commands=validated, rejections=rejections, raw=raw)


def build_response_schema(
    state: ConversationState,
    candidate_flows: list[dict[str, Any]],
    blocked_commands: frozenset[str] = frozenset(),
    *,
    respond_enabled: bool = False,
) -> dict[str, Any]:
    """Constrained-output schema: force the LLM to emit only valid commands/flows/slots.

    - `command` limited to the command vocabulary.
    - `flow` limited to the retrieved candidate flow names (objections cannot be invented).
    - `name` limited to the active collect slot (no inventing customer_name/ptp_date).
    - `value` limited to the slot's enum values when the active slot has a fixed set.
    """
    allowed_commands = VALID_COMMANDS - blocked_commands
    if not respond_enabled:
        allowed_commands = allowed_commands - {"respond"}
    flow_names = [str(c.get("name")) for c in candidate_flows if c.get("name")]
    hints = _active_flow_slot_hints(state)
    active_slot = str(hints[0].get("slot")) if hints and hints[0].get("slot") else None
    value_values = hints[0].get("values") if hints else None
    value_enum = [str(v) for v in value_values] if value_values else None

    item_props: dict[str, Any] = {
        "command": {"type": "string", "enum": sorted(allowed_commands)},
        "reason": {"type": "string"},
        "value": ({"type": "string", "enum": value_enum} if value_enum else {"type": "string"}),
        "name": ({"type": "string", "enum": [active_slot]} if active_slot else {"type": "string"}),
        "text": {"type": "string"},
    }
    if flow_names:
        item_props["flow"] = {"type": "string", "enum": flow_names}

    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": item_props,
            "required": ["command"],
        },
    }


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
    blocked_commands: frozenset[str] = frozenset(),
    catalog_mode: bool = False,
    respond_enabled: bool = False,
    unknown_info_reply: str = "",
) -> CommandParseResult:
    today_iso = resolve_today(state)
    system = build_system_prompt(
        today_iso,
        blocked_commands,
        respond_enabled=respond_enabled,
        unknown_info_reply=unknown_info_reply,
        catalog_mode=catalog_mode,
    )
    user = build_user_prompt(
        text,
        candidate_flows,
        state,
        catalog_mode=catalog_mode,
        respond_enabled=respond_enabled,
    )
    client = llm or create_llm_client()
    schema = build_response_schema(
        state, candidate_flows, blocked_commands, respond_enabled=respond_enabled
    )
    try:
        raw = await client.complete(system, user, json_only=True, response_schema=schema)
    except TypeError:
        # Test doubles / clients that don't accept response_schema.
        raw = await client.complete(system, user, json_only=True)
    return parse_and_validate_commands(
        raw,
        candidate_flows=candidate_flows,
        blocked_commands=blocked_commands,
        catalog_mode=catalog_mode,
        respond_enabled=respond_enabled,
    )
