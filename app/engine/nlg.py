"""Templated NLG — interpolate slots, spoken-form, language select (Sprint 5)."""

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from app.config import TenantConfig
from app.engine.identity_gate import (
    must_block_debt_disclosure,
    slots_for_nlg,
    template_references_debt,
)
from app.schemas.command import Command
from app.schemas.flow import FlowSet, ResponseTemplate
from app.schemas.state import ConversationState

# DECISION NEEDED: confirm v1 languages with product — default Hindi + English + Hinglish.
LANGUAGE_LADDER: tuple[str, ...] = ("hi", "hinglish", "en")

COLLECT_SLOT_REPLY_IDS: dict[str, str] = {
    "ptp_date": "ask_ptp_date",
    "dispute_reason": "ask_dispute_reason",
    "dispute_type": "ask_dispute_type",
    "dispute_claim": "ask_dispute_claim",
    "identity_response": "ask_identity_verification",
    "partial_amount": "ask_partial_amount",
    "payment_rail": "ask_payment_rail",
    "hardship_reason": "ask_hardship_reason",
    "hardship_path": "ask_hardship_path",
    "third_party_borrower_check": "ask_third_party_borrower_check",
    "callback_window": "ask_callback_window",
    "negotiation_request": "ask_negotiation_request",
    # Salary On Time pre-closure collect prompts (clean re-ask on objection resume).
    "sot_identity_response": "sot_greeting",
    "sot_knows_customer": "sot_ask_knows",
    "sot_relation_type": "sot_ask_relation",
    "sot_sibling_type": "sot_sibling_ask",
    "sot_restricted_followup": "sot_restricted_intro",
    "sot_payment_intent": "sot_offer_pre_closure",
    "sot_payment_problem": "sot_ask_reason",
    "sot_payment_intent_2": "sot_push",
    # On/Post-due extra push intents re-ask with a scenario-neutral "try today?" line.
    "sot_payment_intent_3": "sot_push_retry",
    "sot_payment_intent_4": "sot_push_retry",
    "sot_payment_intent_5": "sot_push_retry",
    "sot_commit_timing": "sot_ask_commit_timing",
    "sot_customer_time": "sot_ask_time",
    "sot_ondue_decision": "sot_ondue_push",
    "sot_afterdue_decision": "sot_afterdue_warning",
    "sot_final_confirm": "sot_ask_time",
}

CLARIFY_REPLY_ID = "clarify_general"

# On clarify/cannot_handle, map collect slots to a SHORT re-ask instead of the
# full scripted opener/offer in COLLECT_SLOT_REPLY_IDS (avoids duplicate speech).
CLARIFY_REASK_REPLY_IDS: dict[str, str] = {
    "sot_payment_intent": "sot_push_retry",
    "sot_payment_intent_2": "sot_push_retry",
    "sot_payment_intent_3": "sot_push_retry",
    "sot_payment_intent_4": "sot_push_retry",
    "sot_payment_intent_5": "sot_push_retry",
}

# Multi-variant collect prompts where variant 0 is the long script and 1+ are
# short re-asks — skip variant 0 once that script was already spoken.
CLARIFY_MIN_ROTATION_SLOTS: frozenset[str] = frozenset({"sot_identity_response"})

_SLOT_PATTERN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

_HINDI_ONES = {
    0: "zero",
    1: "ek",
    2: "do",
    3: "teen",
    4: "chaar",
    5: "paanch",
    6: "chhe",
    7: "saat",
    8: "aath",
    9: "nau",
    10: "das",
    11: "gyaarah",
    12: "baarah",
    13: "terah",
    14: "chaudah",
    15: "pandrah",
    16: "solah",
    17: "satrah",
    18: "athaarah",
    19: "unnis",
    20: "bees",
    30: "tees",
    40: "chaalis",
    50: "pachaas",
    60: "saath",
    70: "sattar",
    80: "assi",
    90: "nabbe",
}

_HINDI_MONTHS = {
    1: "January",
    2: "February",
    3: "March",
    4: "April",
    5: "May",
    6: "June",
    7: "July",
    8: "August",
    9: "September",
    10: "October",
    11: "November",
    12: "December",
}


class MissingSlotError(KeyError):
    """Raised when a template references a slot that is not in state."""


@dataclass(frozen=True)
class ResolvedReply:
    """Fully attributed NLG output for audit / analytics."""

    text: str
    reply_id: str | None = None
    variant_index: int | None = None
    language: str | None = None
    tone_register: str | None = None


def normalize_language(locale: str | None, state: ConversationState) -> str:
    prefs = state.slots.get("comms_prefs")
    if isinstance(prefs, dict) and prefs.get("language"):
        lang = str(prefs["language"]).lower()
        if lang in LANGUAGE_LADDER:
            return lang
        if lang.startswith("hi"):
            return "hi"
        if lang.startswith("en"):
            return "en"
    if locale:
        lowered = locale.lower()
        if lowered.startswith("en"):
            return "en"
        if lowered.startswith("hi"):
            return "hi"
    return "hi"


_HINDI_COMPOUND_DAYS: dict[int, str] = {
    21: "ikkees",
    22: "baees",
    23: "teees",
    24: "chaubees",
    25: "pachchees",
    26: "chhabbis",
    27: "sattaees",
    28: "athaees",
    29: "unntees",
    30: "tees",
    31: "ikattis",
}


def _hindi_day(day: int) -> str:
    if day in _HINDI_COMPOUND_DAYS:
        return _HINDI_COMPOUND_DAYS[day]
    return _hindi_under_hundred(day)


def _hindi_under_hundred(value: int) -> str:
    if value < 21 or value % 10 == 0:
        return _HINDI_ONES.get(value, str(value))
    tens = (value // 10) * 10
    ones = value % 10
    return f"{_HINDI_ONES[tens]} {_HINDI_ONES[ones]}"


def spoken_amount_hindi(amount: int) -> str:
    """₹12,400 → baarah hazaar chaar sau rupaye (voice spoken-form)."""
    if amount < 0:
        raise ValueError(f"amount must be non-negative: {amount}")
    if amount == 0:
        return "zero rupaye"

    parts: list[str] = []
    remaining = amount

    if remaining >= 10000000:
        crores = remaining // 10000000
        parts.append(f"{_hindi_under_hundred(crores)} crore")
        remaining %= 10000000
    if remaining >= 100000:
        lakhs = remaining // 100000
        parts.append(f"{_hindi_under_hundred(lakhs)} lakh")
        remaining %= 100000
    if remaining >= 1000:
        thousands = remaining // 1000
        parts.append(f"{_hindi_under_hundred(thousands)} hazaar")
        remaining %= 1000
    if remaining >= 100:
        hundreds = remaining // 100
        parts.append(f"{_hindi_under_hundred(hundreds)} sau")
        remaining %= 100
    if remaining:
        parts.append(_hindi_under_hundred(remaining))

    return " ".join(parts) + " rupaye"


def spoken_date_hindi(value: str | date | datetime) -> str:
    """2026-06-26 → chhabbis June (voice spoken-form)."""
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    else:
        parsed = date.fromisoformat(str(value)[:10])

    day = parsed.day
    month = _HINDI_MONTHS.get(parsed.month, str(parsed.month))
    return f"{_hindi_day(day)} {month}"


def spoken_form_value(value: Any, *, channel: str = "voice") -> str:
    """Convert a slot value to spoken form when channel is voice."""
    if channel != "voice":
        return str(value)

    if isinstance(value, bool):
        return str(value)

    if isinstance(value, int):
        return spoken_amount_hindi(value)

    if isinstance(value, float) and value.is_integer():
        return spoken_amount_hindi(int(value))

    if isinstance(value, (date, datetime)):
        return spoken_date_hindi(value)

    text = str(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return spoken_date_hindi(text)
    if re.fullmatch(r"\d+", text):
        return spoken_amount_hindi(int(text))

    return text


def interpolate_template(template: str, slots: dict[str, Any], *, channel: str = "voice") -> str:
    """Fill {slot} placeholders; raise MissingSlotError if any slot is absent."""
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in slots or slots[key] is None:
            missing.append(key)
            return match.group(0)
        return spoken_form_value(slots[key], channel=channel)

    rendered = _SLOT_PATTERN.sub(replace, template)
    if missing:
        raise MissingSlotError(f"Missing slots for template: {', '.join(missing)}")
    return rendered


def _variants_for_register(
    variants: list[ResponseTemplate],
    tone_register: str,
) -> list[ResponseTemplate]:
    tagged = [variant for variant in variants if variant.tone_register]
    if not tagged:
        return variants
    matching = [variant for variant in tagged if variant.tone_register == tone_register]
    if matching:
        return matching
    standard = [variant for variant in tagged if variant.tone_register == "standard"]
    if standard:
        return standard
    untagged = [variant for variant in variants if not variant.tone_register]
    return untagged or variants


def _variants_for_language(
    variants: list[ResponseTemplate],
    preferred: str,
) -> list[ResponseTemplate]:
    tagged = [variant for variant in variants if variant.language]
    if not tagged:
        return variants

    ladder = [preferred] + [lang for lang in LANGUAGE_LADDER if lang != preferred]
    for lang in ladder:
        matching = [variant for variant in tagged if variant.language == lang]
        if matching:
            return matching
    untagged = [variant for variant in variants if not variant.language]
    return untagged or variants


def pick_variant_with_index(
    variants: list[ResponseTemplate],
    *,
    preferred_language: str,
    rotation_index: int,
    tone_register: str = "standard",
) -> tuple[ResponseTemplate, int]:
    pool = _variants_for_language(variants, preferred_language)
    pool = _variants_for_register(pool, tone_register)
    if not pool:
        raise KeyError("No response variants available")
    index = rotation_index % len(pool)
    return pool[index], index


def pick_variant(
    variants: list[ResponseTemplate],
    *,
    preferred_language: str,
    rotation_index: int,
    tone_register: str = "standard",
) -> ResponseTemplate:
    variant, _ = pick_variant_with_index(
        variants,
        preferred_language=preferred_language,
        rotation_index=rotation_index,
        tone_register=tone_register,
    )
    return variant


def _slot_reask_rotation(state: ConversationState, slot_name: str) -> int:
    """How many times this slot has been re-asked (repair layer F2).

    Drives variant selection so a re-ask sounds different from the first ask:
    index 0 = normal, 1 = simpler + example, 2+ = last-chance phrasing.
    """
    counts = state.slots.get("_repair_counts")
    if isinstance(counts, dict):
        try:
            return int(counts.get(slot_name, 0))
        except (TypeError, ValueError):
            return 0
    return 0


def render_collect_slot_resolved(
    slot_name: str,
    state: ConversationState,
    flows: FlowSet,
    *,
    locale: str = "hi-IN",
    channel: str = "voice",
    tenant_cfg: TenantConfig | None = None,
) -> ResolvedReply:
    reply_id = COLLECT_SLOT_REPLY_IDS.get(slot_name)
    if reply_id and reply_id in flows.responses:
        return render_resolved(
            reply_id,
            state,
            flows,
            locale=locale,
            channel=channel,
            rotation_index=_slot_reask_rotation(state, slot_name),
        )
    if tenant_cfg is not None and slot_name in tenant_cfg.collect_slot_prompts:
        return ResolvedReply(
            text=tenant_cfg.collect_slot_prompts[slot_name],
            reply_id=reply_id,
        )
    raise KeyError(f"No collect prompt for slot: {slot_name}")


def render_collect_slot(
    slot_name: str,
    state: ConversationState,
    flows: FlowSet,
    *,
    locale: str = "hi-IN",
    channel: str = "voice",
    tenant_cfg: TenantConfig | None = None,
) -> str:
    return render_collect_slot_resolved(
        slot_name,
        state,
        flows,
        locale=locale,
        channel=channel,
        tenant_cfg=tenant_cfg,
    ).text


def _commands_include_clarify(commands: list[Command]) -> bool:
    return any(c.command in ("clarify", "cannot_handle") for c in commands)


def _render_clarify_reask(
    slot_name: str,
    state: ConversationState,
    flows: FlowSet,
    *,
    locale: str = "hi-IN",
    channel: str = "voice",
    tenant_cfg: TenantConfig | None = None,
) -> ResolvedReply:
    """Re-ask the awaited slot with a short prompt — never replay the full script."""
    rotation = _slot_reask_rotation(state, slot_name)

    short_id = CLARIFY_REASK_REPLY_IDS.get(slot_name)
    if short_id and short_id in flows.responses:
        return render_resolved(
            short_id,
            state,
            flows,
            locale=locale,
            channel=channel,
            rotation_index=rotation,
        )

    collect_reply_id = COLLECT_SLOT_REPLY_IDS.get(slot_name)
    if slot_name in CLARIFY_MIN_ROTATION_SLOTS and collect_reply_id:
        already_spoke = state.slots.get("last_reply_id") == collect_reply_id
        if already_spoke or rotation >= 1:
            rotation = max(rotation, 1)
    if collect_reply_id and collect_reply_id in flows.responses:
        return render_resolved(
            collect_reply_id,
            state,
            flows,
            locale=locale,
            channel=channel,
            rotation_index=rotation,
        )

    if tenant_cfg is None:
        raise KeyError(f"No clarify re-ask for slot: {slot_name}")
    return render_collect_slot_resolved(
        slot_name,
        state,
        flows,
        locale=locale,
        channel=channel,
        tenant_cfg=tenant_cfg,
    )


def draft_reply_resolved(
    *,
    reply_id: str | None,
    question_slot: str | None,
    commands: list[Command],
    state: ConversationState,
    flows: FlowSet,
    tenant_cfg: TenantConfig,
    locale: str = "hi-IN",
    channel: str = "voice",
    transfer_to_human: bool = False,
) -> ResolvedReply:
    """Build outbound draft from executor output — templates only, never free-generate."""
    repeat_id = state.slots.pop("repeat_reply_id", None)
    if repeat_id and repeat_id in flows.responses:
        return render_resolved(repeat_id, state, flows, locale=locale, channel=channel)

    if reply_id:
        return render_resolved(reply_id, state, flows, locale=locale, channel=channel)

    is_clarify = _commands_include_clarify(commands)

    if question_slot:
        try:
            if is_clarify:
                return _render_clarify_reask(
                    question_slot,
                    state,
                    flows,
                    locale=locale,
                    channel=channel,
                    tenant_cfg=tenant_cfg,
                )
            return render_collect_slot_resolved(
                question_slot,
                state,
                flows,
                locale=locale,
                channel=channel,
                tenant_cfg=tenant_cfg,
            )
        except KeyError:
            return ResolvedReply(text=tenant_cfg.clarify_reply)

    command_types = {cmd.command for cmd in commands}
    if transfer_to_human or "human_handoff" in command_types:
        return ResolvedReply(text=tenant_cfg.care_first_reply)
    if is_clarify:
        last_slot = state.slots.get("last_question_slot")
        if last_slot:
            try:
                return _render_clarify_reask(
                    str(last_slot),
                    state,
                    flows,
                    locale=locale,
                    channel=channel,
                    tenant_cfg=tenant_cfg,
                )
            except KeyError:
                pass
        if CLARIFY_REPLY_ID in flows.responses:
            return render_resolved(CLARIFY_REPLY_ID, state, flows, locale=locale, channel=channel)
        return ResolvedReply(text=tenant_cfg.clarify_reply)

    if CLARIFY_REPLY_ID in flows.responses:
        return render_resolved(CLARIFY_REPLY_ID, state, flows, locale=locale, channel=channel)
    return ResolvedReply(text=tenant_cfg.clarify_reply)


def draft_reply(
    *,
    reply_id: str | None,
    question_slot: str | None,
    commands: list[Command],
    state: ConversationState,
    flows: FlowSet,
    tenant_cfg: TenantConfig,
    locale: str = "hi-IN",
    channel: str = "voice",
    transfer_to_human: bool = False,
) -> str:
    return draft_reply_resolved(
        reply_id=reply_id,
        question_slot=question_slot,
        commands=commands,
        state=state,
        flows=flows,
        tenant_cfg=tenant_cfg,
        locale=locale,
        channel=channel,
        transfer_to_human=transfer_to_human,
    ).text


def render_resolved(
    reply_id: str | None,
    state: ConversationState,
    flows: FlowSet,
    *,
    locale: str = "hi-IN",
    channel: str = "voice",
    rotation_index: int | None = None,
) -> ResolvedReply:
    """Render a templated reply with full variant attribution.

    ``rotation_index`` lets callers pin variant selection (e.g. the per-slot
    re-ask count from the repair layer); when omitted it falls back to the global
    turn counter so replies still vary across turns.
    """
    if reply_id is None:
        return ResolvedReply(text="")

    variants = flows.responses.get(reply_id)
    if not variants:
        raise KeyError(f"Unknown response id: {reply_id}")

    preferred = normalize_language(locale, state)
    tone_register = str(state.slots.get("tone_register") or "standard")
    rotation = state.attempts if rotation_index is None else rotation_index
    variant, variant_index = pick_variant_with_index(
        variants,
        preferred_language=preferred,
        rotation_index=rotation,
        tone_register=tone_register,
    )
    if must_block_debt_disclosure(state.slots) and template_references_debt(variant.text):
        raise MissingSlotError("Debt template blocked before identity verification")
    safe_slots = slots_for_nlg(state.slots)
    text = interpolate_template(variant.text, safe_slots, channel=channel)
    return ResolvedReply(
        text=text,
        reply_id=reply_id,
        variant_index=variant_index,
        language=variant.language or preferred,
        tone_register=variant.tone_register or tone_register,
    )


def render(
    reply_id: str | None,
    state: ConversationState,
    flows: FlowSet,
    *,
    locale: str = "hi-IN",
    channel: str = "voice",
) -> str:
    """Render a templated reply from flow YAML — never free-generate text."""
    return render_resolved(
        reply_id,
        state,
        flows,
        locale=locale,
        channel=channel,
    ).text
