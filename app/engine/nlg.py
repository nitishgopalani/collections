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
    "sot_payment_intent": "sot_offer_ask_today",
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
    # PaisaLo collect re-asks (P5).
    "plo_identity_response": "plo_identity_ask",
    "plo_payment_intent": "plo_reask_intent",
    "plo_consent_2min": "plo_npa_consent_ask",
    "plo_timeline": "plo_reask_timeline",
}

CLARIFY_REPLY_ID = "clarify_general"

# Per-reply_id utterance counts for attempt-indexed templates (Phase 4).
REPLY_COUNTS_KEY = "_reply_counts"


def max_attempt_for_reply(flows: FlowSet, reply_id: str) -> int | None:
    """Highest ``attempt`` tag on a reply_id group, or None if untagged."""
    variants = flows.responses.get(reply_id) or []
    attempts = [int(v.attempt) for v in variants if v.attempt is not None]
    return max(attempts) if attempts else None


def clear_reply_counts(slots: dict[str, Any]) -> None:
    """Drop attempt counters (call closed)."""
    slots.pop(REPLY_COUNTS_KEY, None)

# On clarify/cannot_handle, map collect slots to a SHORT re-ask instead of the
# full scripted opener/offer in COLLECT_SLOT_REPLY_IDS (avoids duplicate speech).
CLARIFY_REASK_REPLY_IDS: dict[str, str] = {
    "sot_payment_intent": "sot_push_retry",
    "sot_payment_intent_2": "sot_push_retry",
    "sot_payment_intent_3": "sot_push_retry",
    "sot_payment_intent_4": "sot_push_retry",
    "sot_payment_intent_5": "sot_push_retry",
    "plo_payment_intent": "plo_reask_intent",
    "plo_timeline": "plo_reask_timeline",
    "plo_consent_2min": "plo_reask_consent",
}

# Multi-variant collect prompts where variant 0 is the long script and 1+ are
# short re-asks — skip variant 0 once that script was already spoken.
CLARIFY_MIN_ROTATION_SLOTS: frozenset[str] = frozenset({"sot_identity_response"})

_SLOT_PATTERN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

_HINDI_ONES = {
    0: "शून्य",
    1: "एक",
    2: "दो",
    3: "तीन",
    4: "चार",
    5: "पाँच",
    6: "छह",
    7: "सात",
    8: "आठ",
    9: "नौ",
    10: "दस",
    11: "ग्यारह",
    12: "बारह",
    13: "तेरह",
    14: "चौदह",
    15: "पंद्रह",
    16: "सोलह",
    17: "सत्रह",
    18: "अठारह",
    19: "उन्नीस",
    20: "बीस",
    21: "इक्कीस",
    22: "बाईस",
    23: "तेईस",
    24: "चौबीस",
    25: "पच्चीस",
    26: "छब्बीस",
    27: "सत्ताईस",
    28: "अट्ठाईस",
    29: "उनतीस",
    30: "तीस",
    31: "इकतीस",
    32: "बत्तीस",
    33: "तैंतीस",
    34: "चौंतीस",
    35: "पैंतीस",
    36: "छत्तीस",
    37: "सैंतीस",
    38: "अड़तीस",
    39: "उनतालीस",
    40: "चालीस",
    41: "इकतालीस",
    42: "बयालीस",
    43: "तैंतालीस",
    44: "चवालीस",
    45: "पैंतालीस",
    46: "छियालीस",
    47: "सैंतालीस",
    48: "अड़तालीस",
    49: "उनचास",
    50: "पचास",
    51: "इक्यावन",
    52: "बावन",
    53: "तिरपन",
    54: "चौवन",
    55: "पचपन",
    56: "छप्पन",
    57: "सत्तावन",
    58: "अट्ठावन",
    59: "उनसठ",
    60: "साठ",
    61: "इकसठ",
    62: "बासठ",
    63: "तिरसठ",
    64: "चौंसठ",
    65: "पैंसठ",
    66: "छियासठ",
    67: "सड़सठ",
    68: "अड़सठ",
    69: "उनहत्तर",
    70: "सत्तर",
    71: "इकहत्तर",
    72: "बहत्तर",
    73: "तिहत्तर",
    74: "चौहत्तर",
    75: "पचहत्तर",
    76: "छिहत्तर",
    77: "सतहत्तर",
    78: "अठहत्तर",
    79: "उनासी",
    80: "अस्सी",
    81: "इक्यासी",
    82: "बयासी",
    83: "तिरासी",
    84: "चौरासी",
    85: "पचासी",
    86: "छियासी",
    87: "सत्तासी",
    88: "अट्ठासी",
    89: "नवासी",
    90: "नब्बे",
    91: "इक्यानवे",
    92: "बानवे",
    93: "तिरानवे",
    94: "चौरानवे",
    95: "पचानवे",
    96: "छियानवे",
    97: "सत्तानवे",
    98: "अट्ठानवे",
    99: "निन्यानवे",
}

_HINDI_MONTHS = {
    1: "जनवरी",
    2: "फ़रवरी",
    3: "मार्च",
    4: "अप्रैल",
    5: "मई",
    6: "जून",
    7: "जुलाई",
    8: "अगस्त",
    9: "सितंबर",
    10: "अक्टूबर",
    11: "नवंबर",
    12: "दिसंबर",
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


def _hindi_day(day: int) -> str:
    return _hindi_under_hundred(day)


def _hindi_under_hundred(value: int) -> str:
    if value in _HINDI_ONES:
        return _HINDI_ONES[value]
    if value < 0:
        raise ValueError(f"value must be non-negative: {value}")
    tens = (value // 10) * 10
    ones = value % 10
    return f"{_HINDI_ONES[tens]} {_HINDI_ONES[ones]}"


def spoken_amount_hindi(amount: int) -> str:
    """4500 → चार हज़ार पाँच सौ रुपये (Devanagari spoken-form, no ₹)."""
    if amount < 0:
        raise ValueError(f"amount must be non-negative: {amount}")
    if amount == 0:
        return "शून्य रुपये"

    parts: list[str] = []
    remaining = amount

    if remaining >= 10000000:
        crores = remaining // 10000000
        parts.append(f"{_hindi_under_hundred(crores)} करोड़")
        remaining %= 10000000
    if remaining >= 100000:
        lakhs = remaining // 100000
        parts.append(f"{_hindi_under_hundred(lakhs)} लाख")
        remaining %= 100000
    if remaining >= 1000:
        thousands = remaining // 1000
        parts.append(f"{_hindi_under_hundred(thousands)} हज़ार")
        remaining %= 1000
    if remaining >= 100:
        hundreds = remaining // 100
        parts.append(f"{_hindi_under_hundred(hundreds)} सौ")
        remaining %= 100
    if remaining:
        parts.append(_hindi_under_hundred(remaining))

    return " ".join(parts) + " रुपये"


def spoken_date_hindi(value: str | date | datetime) -> str:
    """2026-06-26 → छब्बीस जून (Devanagari spoken-form)."""
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    else:
        parsed = date.fromisoformat(str(value)[:10])

    day = parsed.day
    month = _HINDI_MONTHS.get(parsed.month, str(parsed.month))
    return f"{_hindi_day(day)} {month}"


def spoken_days_hindi(value: int) -> str:
    """G-B4-01 / G1: 5 → "पाँच", 15 → "पंद्रह", 30 → "तीस" (Devanagari, no रुपये).

    Used via the derived ``days_past_due_words`` NLG slot so a which-EMI /
    postdue greeting line reads "पंद्रह दिनों से बकाया है" instead of the
    amount helper's wrong "पाँच रुपये दिनों से". Negative DPD (predue) is
    spoken as its absolute value — the template's own words carry the
    "before due date" framing.
    """
    n = abs(int(value))
    if n < 100:
        return _hindi_under_hundred(n)
    return spoken_amount_hindi(n).removesuffix(" रुपये")


def spoken_digits_hindi(value: str | int) -> str:
    """Digit-by-digit phone: 9180… → नौ एक आठ शून्य … (Devanagari, no Latin)."""
    digits = [c for c in str(value) if c.isdigit()]
    if not digits:
        return ""
    return " ".join(_HINDI_ONES[int(d)] for d in digits)


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


def interpolate_template(
    template: str,
    slots: dict[str, Any],
    *,
    channel: str = "voice",
    persona_voice: str | None = None,
) -> str:
    """Fill {slot} / {G} placeholders; raise MissingSlotError if any slot is absent."""
    from app.engine.gender import resolve_gender_tokens

    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key == "G":
            return match.group(0)
        if key not in slots or slots[key] is None:
            missing.append(key)
            return match.group(0)
        return spoken_form_value(slots[key], channel=channel)

    rendered = _SLOT_PATTERN.sub(replace, template)
    if missing:
        raise MissingSlotError(f"Missing slots for template: {', '.join(missing)}")
    voice = persona_voice or slots.get("voice_id")
    rendered = resolve_gender_tokens(rendered, str(voice) if voice else None)
    return rendered.replace("₹", "")


def _gendered_system_line(text: str, state: ConversationState) -> str:
    from app.engine.gender import resolve_gender_tokens

    voice = str(state.slots.get("voice_id") or "") or None
    return resolve_gender_tokens(text, voice).replace("₹", "")


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


def _pick_attempt_variant(
    pool: list[ResponseTemplate],
    *,
    play_n: int,
) -> tuple[ResponseTemplate, int]:
    """Deterministic attempt pick: exact match, else highest defined attempt."""
    tagged = [v for v in pool if v.attempt is not None]
    if not tagged:
        raise KeyError("No attempt-tagged variants")
    exact = [v for v in tagged if int(v.attempt) == play_n]
    chosen = exact[0] if exact else max(tagged, key=lambda v: int(v.attempt or 0))
    # Stable index within the filtered pool for audit attribution.
    try:
        index = pool.index(chosen)
    except ValueError:
        index = 0
    return chosen, index


def pick_variant_with_index(
    variants: list[ResponseTemplate],
    *,
    preferred_language: str,
    rotation_index: int,
    tone_register: str = "standard",
    play_n: int | None = None,
) -> tuple[ResponseTemplate, int]:
    pool = _variants_for_language(variants, preferred_language)
    pool = _variants_for_register(pool, tone_register)
    if not pool:
        raise KeyError("No response variants available")
    # Attempt-indexed groups replace rotation/random pick entirely.
    if play_n is not None and any(v.attempt is not None for v in pool):
        return _pick_attempt_variant(pool, play_n=play_n)
    if any(v.attempt is not None for v in variants):
        # Language/tone filter dropped tags — fall back on full tagged set.
        tagged_pool = _variants_for_register(
            _variants_for_language(variants, preferred_language),
            tone_register,
        )
        if any(v.attempt is not None for v in tagged_pool) and play_n is not None:
            return _pick_attempt_variant(tagged_pool, play_n=play_n)
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


def render_short_reask(
    slot_name: str,
    state: ConversationState,
    flows: FlowSet,
    *,
    locale: str = "hi-IN",
    channel: str = "voice",
    tenant_cfg: TenantConfig | None = None,
) -> ResolvedReply:
    """Public wrapper: short retry prompt for the awaited collect slot."""
    return _render_clarify_reask(
        slot_name,
        state,
        flows,
        locale=locale,
        channel=channel,
        tenant_cfg=tenant_cfg,
    )


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
        # Prefer last_reply_id; also honor reply counts so a barged first
        # egress still counts as "uttered" once the brain produced it.
        already_spoke = state.slots.get("last_reply_id") == collect_reply_id
        counts = state.slots.get(REPLY_COUNTS_KEY) or {}
        try:
            if int(counts.get(collect_reply_id, 0) or 0) >= 1:
                already_spoke = True
        except (TypeError, ValueError):
            pass
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


def _render_utter_chain(
    utter_chain: list[str],
    state: ConversationState,
    flows: FlowSet,
    *,
    locale: str,
    channel: str,
) -> ResolvedReply | None:
    """Join consecutive utter templates (offer facts + ask) into one spoken draft."""
    ids = [u for u in utter_chain if u and u in flows.responses]
    if len(ids) < 2:
        return None
    parts: list[str] = []
    primary: ResolvedReply | None = None
    for uid in ids:
        rendered = render_resolved(uid, state, flows, locale=locale, channel=channel)
        if primary is None:
            primary = rendered
        if rendered.text.strip():
            parts.append(rendered.text.strip())
    if primary is None or not parts:
        return None
    return ResolvedReply(
        text=" ".join(parts),
        reply_id=primary.reply_id,
        variant_index=primary.variant_index,
        language=primary.language,
        tone_register=primary.tone_register,
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
    utter_chain: list[str] | None = None,
) -> ResolvedReply:
    """Build outbound draft from executor output — templates only, never free-generate."""
    repeat_id = state.slots.pop("repeat_reply_id", None)
    if repeat_id and repeat_id in flows.responses:
        return render_resolved(repeat_id, state, flows, locale=locale, channel=channel)

    is_clarify = _commands_include_clarify(commands)

    # B1: clarify/cannot_handle on identity (and other min-rotation slots) must
    # short-reask even when the executor also stamped reply_id=sot_greeting.
    # Without this, barged greetings replay the full script (call 3e4b5e36).
    if (
        is_clarify
        and question_slot
        and question_slot in CLARIFY_MIN_ROTATION_SLOTS
    ):
        try:
            return _render_clarify_reask(
                question_slot,
                state,
                flows,
                locale=locale,
                channel=channel,
                tenant_cfg=tenant_cfg,
            )
        except KeyError:
            pass

    chained = _render_utter_chain(
        list(utter_chain or []),
        state,
        flows,
        locale=locale,
        channel=channel,
    )
    if chained is not None:
        return chained

    if reply_id:
        return render_resolved(reply_id, state, flows, locale=locale, channel=channel)

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
            return ResolvedReply(
                text=_gendered_system_line(tenant_cfg.clarify_reply, state)
            )

    command_types = {cmd.command for cmd in commands}
    if transfer_to_human or "human_handoff" in command_types:
        return ResolvedReply(text=_gendered_system_line(tenant_cfg.care_first_reply, state))
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
        return ResolvedReply(text=_gendered_system_line(tenant_cfg.clarify_reply, state))

    if CLARIFY_REPLY_ID in flows.responses:
        return render_resolved(CLARIFY_REPLY_ID, state, flows, locale=locale, channel=channel)
    return ResolvedReply(text=_gendered_system_line(tenant_cfg.clarify_reply, state))


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
    play_n: int | None = None
    if any(v.attempt is not None for v in variants):
        counts = state.slots.get(REPLY_COUNTS_KEY) or {}
        prior = 0
        if isinstance(counts, dict):
            try:
                prior = int(counts.get(reply_id, 0))
            except (TypeError, ValueError):
                prior = 0
        play_n = prior + 1
    variant, variant_index = pick_variant_with_index(
        variants,
        preferred_language=preferred,
        rotation_index=rotation,
        tone_register=tone_register,
        play_n=play_n,
    )
    if must_block_debt_disclosure(state.slots) and template_references_debt(variant.text):
        raise MissingSlotError("Debt template blocked before identity verification")
    safe_slots = slots_for_nlg(state.slots)
    text = interpolate_template(
        variant.text,
        safe_slots,
        channel=channel,
        persona_voice=str(state.slots.get("voice_id") or "") or None,
    )
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
