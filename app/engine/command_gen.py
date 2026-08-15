import asyncio
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
        "compose",
    }
)
ALLOWED_COMMAND_FIELDS: frozenset[str] = frozenset(
    {"command", "flow", "name", "value", "reason", "text", "fragments", "oof_class"}
)
OOF_CLASSES: frozenset[str] = frozenset(
    {
        "payment_assertion",
        "complaint",
        "call_context",
        "related_oof",
        "irrelevant",
        "prompt_injection",
        "repeated_diversion",
        "vulnerability",
        "third_party",
    }
)
# W2-5: compose-selection few-shots (prompt only — no new machinery).
COMPOSE_FEW_SHOTS = (
    "Prefer compose over respond. compose picks <=2 fragment ids + oof_class. "
    "Omit oof_class on normal-flow turns. respond is last-resort escape hatch "
    "only when no fragment applies. "
    "FEW-SHOTS: "
    '(1) complaint "yeh company bekar hai" / "tumhari company fraud hai" -> '
    '[{"command":"compose","fragments":["ack_neutral","fact_grievance"],'
    '"oof_class":"complaint"}]. '
    '(2) irrelevant "mausam kaisa hai?" / "aaj ka match kaun jeeta" / weather/'
    'cricket/politics -> '
    '[{"command":"compose","fragments":["irrelevant_redirect"],'
    '"oof_class":"irrelevant","related":false,'
    '"ack_text":"आप शायद मौसम के बारे में पूछ रहे हैं"}]. '
    "On OOF turns set related (bool) + ack_text (आप शायद … के बारे में, "
    "<=12 words, no names/numbers/answers). related=true means loan-adjacent "
    "unknown — do not invent facts. "
    '(3) account/branch facts "office kahan se?" / "branch kahan hai?" / '
    '"branch ka number?" -> '
    '[{"command":"compose","fragments":["fact_branch"],'
    '"oof_class":"call_context"}]. '
    '(4) caller-identity "aap kaun bol rahe hain" / "aap bol kaun rahe hain" / '
    '"who are you" -> '
    '[{"command":"compose","fragments":["fact_caller_identity"],'
    '"oof_class":"call_context"}]. '
    "Pick compose ids ONLY from fragment_index. NEVER invent fragment ids "
    "(no who_are_you, no fact_agent_intro). "
    "NEVER respond/unknown-info when a fact fragment covers the question. "
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
        # G-B4-02: committed_date is a per-call commitment the LLM may set
        # when the borrower gives a specific date (see coerce_committed_date).
        "committed_date",
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
        "tts_model",
        "tts_pace",
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
        "committed_date",
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
        "compose",
    )
    allowed = [
        c
        for c in command_vocab
        if c not in blocked_commands
        and (c not in {"respond", "compose"} or respond_enabled)
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
            parts.append(COMPOSE_FEW_SHOTS)
            parts.append(
                "compose.fragments must be ids from fragment_index in the user "
                "payload (id + answers tags). NEVER invent ids. "
            )
            parts.append(
                "If no compose fragment covers the question, output "
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
            "Resolve relative dates (kal, parso, N din baad, next week) to ISO "
            "YYYY-MM-DD on committed_date — NEVER on payment_intent / "
            "plo_payment_intent. ",
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
    fragment_index: list[dict[str, Any]] | None = None,
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
    if fragment_index:
        payload["fragment_index"] = fragment_index
        payload["compose_note"] = (
            "compose.fragments must be ids from fragment_index. "
            "Match the transcript to answers tags. NEVER invent fragment ids."
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
        # ---- PaisaLo collect slots ----
        "plo_identity_response": {
            "slot": "plo_identity_response",
            "values": ["confirmed", "denied", "wrong_number"],
            "map_examples": {
                "haan/ji/main bol raha hoon/yes/speaking": "confirmed",
                "nahi/galat number/wrong person": "denied",
            },
        },
        "plo_consent_2min": {
            "slot": "plo_consent_2min",
            "values": ["yes", "no"],
            "map_examples": {"haan/theek hai/ok": "yes", "nahi/abhi nahi": "no"},
        },
        "plo_payment_intent": {
            "slot": "plo_payment_intent",
            "values": ["willing", "refused", "refuse", "later"],
            "note": (
                "ALWAYS one of willing / refused / later — NEVER an ISO date or "
                "prose here. Dates (kal, parso, N din baad, 15 August) go to "
                "committed_date as YYYY-MM-DD together with willing. "
                "'baad mein' / 'jald hi' with no concrete day = later (no date)."
            ),
            "map_examples": {
                "haan/kar dunga/de dunga/theek hai": "willing",
                "nahi/nahi kar paunga/nahi dunga": "refused",
                "baad mein/jald hi/jaldi": "later",
            },
        },
        "plo_timeline": {
            "slot": "plo_timeline",
            "values": ["willing", "specific_date", "refuse", "refused", "later"],
            "note": (
                "Commit timing. A concrete date goes to committed_date (ISO) and "
                "this slot = specific_date. NEVER put the ISO date in this slot."
            ),
            "map_examples": {
                "aaj/abhi kar dunga": "willing",
                "15 August/kal/10 din baad": "specific_date",
                "nahi de sakta": "refuse",
                "baad mein/jald hi": "later",
            },
        },
    }
    hint = hints.get(step.collect)
    if hint is None:
        return [{"slot": step.collect, "note": "set_slot with an appropriate value"}]
    return [hint]


# Prompt-hint enums (same values the LLM is told). Merged with collect-slot
# decide branches so the parse guard is flow-metadata, not a tenant if.
_HINT_ENUMS: dict[str, frozenset[str]] = {
    "identity_confirmed": frozenset({"confirmed", "denied"}),
    "payment_intent": frozenset({"willing", "dispute"}),
    "payment_ack": frozenset({"paid"}),
    "test_identity_intent": frozenset({"confirmed", "denied"}),
    "test_payment_intent": frozenset({"willing", "dispute"}),
    "test_paid_intent": frozenset({"paid"}),
    "sot_identity_response": frozenset({"confirmed", "relation", "denied"}),
    "sot_knows_customer": frozenset({"true", "false"}),
    "sot_sibling_type": frozenset({"real", "cousin"}),
    "sot_restricted_followup": frozenset(
        {"wants_details", "alternate_number", "unavailable"}
    ),
    "sot_payment_intent": frozenset({"willing", "already_paid", "refused"}),
    "sot_payment_intent_2": frozenset({"willing", "refused"}),
    "sot_payment_intent_3": frozenset({"willing", "refused"}),
    "sot_payment_intent_4": frozenset({"willing", "refused"}),
    "sot_payment_intent_5": frozenset({"willing", "refused"}),
    "sot_commit_timing": frozenset(
        {"today", "tomorrow", "before_due", "on_due", "after_due"}
    ),
    "sot_ondue_decision": frozenset({"pay_today", "later"}),
    "sot_afterdue_decision": frozenset({"pay_today", "later"}),
    "sot_final_confirm": frozenset({"yes", "no"}),
    "plo_identity_response": frozenset({"confirmed", "denied", "wrong_number"}),
    "plo_consent_2min": frozenset({"yes", "no"}),
    "plo_payment_intent": frozenset({"willing", "refused", "refuse", "later"}),
    "plo_timeline": frozenset(
        {"willing", "specific_date", "refuse", "refused", "later"}
    ),
}
_DECIDE_ENUM_SKIP = frozenset({"true", "false", "null", "none"})
_DECIDE_EQ_RE = re.compile(
    r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*==\s*(.+)$"
)


def slot_enum_values() -> dict[str, frozenset[str]]:
    """Allowed values per collect slot: hint catalog ∪ flow decide metadata."""
    merged: dict[str, set[str]] = {k: set(v) for k, v in _HINT_ENUMS.items()}
    flows = get_flow_set()
    collect = {
        step.collect
        for flow in flows.flows.values()
        for step in flow.steps
        if step.collect
    }
    for flow in flows.flows.values():
        for step in flow.steps:
            for branch in step.decide or []:
                expr = getattr(branch, "if_", None) or ""
                m = _DECIDE_EQ_RE.match(expr)
                if not m:
                    continue
                slot, raw = m.group(1), m.group(2).strip().strip("'\"")
                if slot not in collect:
                    continue
                low = raw.lower()
                if low in _DECIDE_ENUM_SKIP or re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
                    continue
                merged.setdefault(slot, set()).add(raw)
    return {k: frozenset(v) for k, v in merged.items()}


@dataclass
class CommandParseResult:
    commands: list[Command] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)
    raw: str = ""
    # W2-3 router contract (same LLM call — invariant #7). Telemetry-only
    # (invariant #6 — confidence is NEVER a Commitment-Gate input). Omitted
    # (None) on normal-flow turns so the parse surface stays disciplined.
    # oof_class: one of 9 values (payment_assertion, complaint, call_context,
    # related_oof, irrelevant, prompt_injection, repeated_diversion,
    # vulnerability, third_party) — None on normal turns.
    oof_class: str | None = None
    # subclass refines oof_class (e.g. prompt_injection, repeated_diversion).
    oof_subclass: str | None = None
    # secondary_intents: additional intents detected on the same turn
    # (multi-intent precedence per invariant #5).
    secondary_intents: list[str] = field(default_factory=list)
    # confidence: LLM confidence (0..1) — TELEMETRY ONLY, never a gate input.
    confidence: float | None = None
    # W2-4b D3: LLM returned a flow outside the scoped catalog that is
    # still in the full tenant catalog. Accepted (telemetry week); logged.
    scope_miss: bool = False
    # F2: key-alias recoveries applied this parse (e.g. "text->value").
    alias_used: list[str] = field(default_factory=list)
    # W3-4: command_gen 429/timeout — empty commands, call survives.
    degraded: bool = False
    # OOF-STACK L1: same LLM call. related=None on normal turns.
    related: bool | None = None
    ack_text: str | None = None


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
    full_catalog_names: frozenset[str] | None = None,
) -> CommandParseResult:
    """Parse LLM JSON output; reject unknown/blocked commands/fields; malformed → clarify."""
    allowed_slots = known_slot_names()
    enum_map = slot_enum_values()
    # Tier-2 catalog mode: start_flow must be in the offered catalog (enforces
    # deflection filtering even when the LLM client ignores response_schema).
    # Legacy digression/RAG keeps the prior known_flow_names-only check.
    # W2-4b D3: reject means "not in SCOPED set". Escape valve: if the flow
    # is in the full tenant catalog, accept and set scope_miss=true.
    catalog_names = _candidate_flow_names(candidate_flows or [])
    rejections: list[str] = []
    alias_used: list[str] = []
    scope_miss = False
    wrapper_oof: str | None = None
    wrapper_subclass: str | None = None
    wrapper_secondary: list[str] = []
    wrapper_confidence: float | None = None
    wrapper_related: bool | None = None
    wrapper_ack: str | None = None

    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError:
        logger.info("command_gen: invalid JSON from LLM raw=%s", (raw or "")[:300])
        return CommandParseResult(commands=[Command(command="clarify")], raw=raw)

    if isinstance(data, dict):
        raw_oof = data.get("oof_class")
        if isinstance(raw_oof, str) and raw_oof in OOF_CLASSES:
            wrapper_oof = raw_oof
        raw_sub = data.get("oof_subclass")
        if isinstance(raw_sub, str) and raw_sub:
            wrapper_subclass = raw_sub
        raw_sec = data.get("secondary_intents")
        if isinstance(raw_sec, list):
            wrapper_secondary = [str(x) for x in raw_sec if x]
        raw_conf = data.get("confidence")
        if isinstance(raw_conf, (int, float)):
            wrapper_confidence = float(raw_conf)
        raw_rel = data.get("related")
        if isinstance(raw_rel, bool):
            wrapper_related = raw_rel
        raw_ack = data.get("ack_text")
        if isinstance(raw_ack, str) and raw_ack.strip():
            wrapper_ack = raw_ack.strip()
        data = data.get("commands", data.get("command"))
    if isinstance(data, list) and data and isinstance(data[0], dict):
        if wrapper_related is None and isinstance(data[0].get("related"), bool):
            wrapper_related = data[0]["related"]
        if wrapper_ack is None and isinstance(data[0].get("ack_text"), str):
            wrapper_ack = data[0]["ack_text"].strip() or None
    if not isinstance(data, list) or not data:
        logger.info("command_gen: no commands parsed raw=%s", (raw or "")[:300])
        empty_ok = wrapper_oof in {"irrelevant", "related_oof"} or wrapper_related is not None
        return CommandParseResult(
            commands=[] if empty_ok else [Command(command="clarify")],
            raw=raw,
            oof_class=wrapper_oof,
            oof_subclass=wrapper_subclass,
            secondary_intents=wrapper_secondary,
            confidence=wrapper_confidence,
            related=wrapper_related,
            ack_text=wrapper_ack,
        )

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
                if full_catalog_names and str(flow_name) in full_catalog_names:
                    scope_miss = True
                    logger.info(
                        "command_gen: scope_miss accepted flow=%s", flow_name
                    )
                else:
                    reason = f"rejected out-of-catalog flow {flow_name}"
                    rejections.append(reason)
                    logger.info("command_gen: %s", reason)
                    continue
        if command_type == "set_slot":
            if cleaned.get("value") is None and item.get("text") is not None:
                cleaned["value"] = item["text"]
                alias_used.append("text->value")
                logger.info("command_gen: alias_used=text->value")
            cleaned.pop("text", None)
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
            allowed = enum_map.get(str(slot_name))
            if allowed:
                raw_val = str(cleaned.get("value") or "").strip()
                if raw_val not in allowed and raw_val.lower() not in {a.lower() for a in allowed}:
                    reason = f"slot_enum_violation slot={slot_name} value={raw_val!r}"
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
        if command_type == "compose":
            if not respond_enabled:
                reason = "rejected compose (respond_enabled=false)"
                rejections.append(reason)
                logger.info("command_gen: %s", reason)
                continue
            frags = item.get("fragments")
            if isinstance(frags, str):
                frags = [frags]
            if not isinstance(frags, list):
                frags = []
            cleaned["fragments"] = [str(f) for f in frags if f][:2]
            oof = item.get("oof_class")
            if isinstance(oof, str) and oof in OOF_CLASSES:
                cleaned["oof_class"] = oof
            if not cleaned["fragments"] and cleaned.get("oof_class") != "irrelevant":
                reason = "rejected empty compose"
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

    # respond may co-exist with set_slot; never with start_flow (drop respond).
    if any(c.command == "start_flow" for c in validated) and any(
        c.command == "respond" for c in validated
    ):
        validated = [c for c in validated if c.command != "respond"]
        rejections.append("dropped respond co-occurring with start_flow")
        logger.info("command_gen: dropped respond co-occurring with start_flow")
    # compose replaces respond when both fire (invariant #4).
    if any(c.command == "compose" for c in validated) and any(
        c.command == "respond" for c in validated
    ):
        validated = [c for c in validated if c.command != "respond"]
        rejections.append("dropped respond co-occurring with compose")
        logger.info("command_gen: dropped respond co-occurring with compose")

    oof_class = wrapper_oof
    if oof_class is None:
        for cmd in validated:
            if cmd.oof_class and cmd.oof_class in OOF_CLASSES:
                oof_class = cmd.oof_class
                break

    if not validated:
        return CommandParseResult(
            commands=[Command(command="clarify")],
            rejections=rejections,
            raw=raw,
            oof_class=oof_class,
            oof_subclass=wrapper_subclass,
            secondary_intents=wrapper_secondary,
            confidence=wrapper_confidence,
            scope_miss=scope_miss,
            alias_used=alias_used,
            related=wrapper_related,
            ack_text=wrapper_ack,
        )
    return CommandParseResult(
        commands=validated,
        rejections=rejections,
        raw=raw,
        oof_class=oof_class,
        oof_subclass=wrapper_subclass,
        secondary_intents=wrapper_secondary,
        confidence=wrapper_confidence,
        scope_miss=scope_miss,
        alias_used=alias_used,
        related=wrapper_related,
        ack_text=wrapper_ack,
    )


def parse_validate_success(result: CommandParseResult) -> bool:
    """F6: D2 cache write-through only when parse+validate produced real commands.

    Clarify-only fallbacks and any field/slot rejection are NOT success — those
    were the e1d5d837 t7/t9 loop (cached rejected ``set_slot text=no``).
    """
    if not (result.raw or "").strip():
        return False
    if result.rejections:
        return False
    cmds = result.commands or []
    if not cmds:
        return False
    if all(c.command == "clarify" for c in cmds):
        return False
    return True


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
        allowed_commands = allowed_commands - {"respond", "compose"}
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
        "fragments": {"type": "array", "items": {"type": "string"}},
        "oof_class": {"type": "string", "enum": sorted(OOF_CLASSES)},
        "related": {"type": "boolean"},
        "ack_text": {"type": "string"},
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


def is_llm_degrade_error(exc: BaseException) -> bool:
    """429 / timeout / resource-exhausted — survive with a deterministic turn."""
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return True
    name = type(exc).__name__.lower()
    if "timeout" in name or "ratelimit" in name:
        return True
    text = str(exc).lower()
    return any(
        token in text
        for token in ("429", "timeout", "timed out", "resource exhausted", "resourceexhausted")
    )


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
    full_catalog_names: frozenset[str] | None = None,
) -> CommandParseResult:
    today_iso = resolve_today(state)
    fragment_index: list[dict[str, Any]] = []
    if respond_enabled:
        from app.engine.catalog import infer_scenario_key
        from app.engine.fragment_library import build_fragment_index

        scenario = infer_scenario_key(state, get_flow_set())
        fragment_index = build_fragment_index(state.tenant_id, scenario)
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
        fragment_index=fragment_index or None,
    )
    client = llm or create_llm_client()
    schema = build_response_schema(
        state, candidate_flows, blocked_commands, respond_enabled=respond_enabled
    )
    try:
        try:
            raw = await client.complete(system, user, json_only=True, response_schema=schema)
        except TypeError:
            # Test doubles / clients that don't accept response_schema.
            raw = await client.complete(system, user, json_only=True)
    except Exception as exc:
        if is_llm_degrade_error(exc):
            logger.warning("command_gen degraded err=%s", exc)
            return CommandParseResult(
                commands=[],
                rejections=["llm_degraded"],
                raw="",
                degraded=True,
            )
        raise
    return parse_and_validate_commands(
        raw,
        candidate_flows=candidate_flows,
        blocked_commands=blocked_commands,
        catalog_mode=catalog_mode,
        respond_enabled=respond_enabled,
        full_catalog_names=full_catalog_names,
    )
