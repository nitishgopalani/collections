"""Scripted-tenant coercions — profile-driven, tenant-agnostic.

Formerly the ``_coerce_sot_*`` block in ``turn.py``. Cue lists and slot sets come
from :class:`TenantRuntimeProfile`. The shared inability regex stays here (language
level, not tenant level).
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.engine.tenant_profile import TenantRuntimeProfile
from app.schemas.command import Command
from app.schemas.flow import FlowSet
from app.schemas.state import ConversationState

# Soft inability: "nahi ... paaunga/sakta/can't" within a short window (ASR-tolerant).
# Shared across scripted tenants — not loaded from YAML.
INABILITY_RE = re.compile(
    r"(नहीं|नही|नहि|\bnahi\b|\bnahin\b|\bnhi\b|\bno\b)"
    r".{0,30}?"
    r"(पा[एऊउ]|सक[तनू]|paung|payeg|paeg|sakt|sakun|can'?t|cannot|unable|not able)",
    re.IGNORECASE | re.UNICODE | re.DOTALL,
)

# Day-shift, not refusal: "nahi aaj nahi kal karunga" (Checkpoint-0 C2 / R2).
_DAY_SHIFT_RE = re.compile(
    r"(aaj\s+nahi\s+kal|nahi\s+\w+\s+nahi\s+kal|"
    r"आज\s+नहीं\s+कल|नहीं\s+\S+\s+नहीं\s+कल)",
    re.IGNORECASE | re.UNICODE,
)

# F4: unwillingness ("I will not") — distinct from inability ("I cannot").
UNWILLINGNESS_RE = re.compile(
    r"(नहीं|नही|नहि|\bnahi\b|\bnahin\b|\bnhi\b|\bno\b)"
    r".{0,30}?"
    r"(कर[ूँूु]+ं?ग[ाी]|दू[ँूु]+ं?ग[ाी]|karung[ai]|karoo?nga|dung[ai]|doong[ai])",
    re.IGNORECASE | re.UNICODE | re.DOTALL,
)

# Identity D1 skip fillers (yes+name). Bot-echo leftovers (थी/था) block skip.
_IDENTITY_FILLERS = frozenset({
    "main", "mai", "mein", "me", "hoon", "hun", "hi", "ji",
    "bol", "raha", "rahi", "rahe", "speaking",
    "मैं", "हूँ", "हूं", "ही", "जी", "बोल", "रहा", "रही", "रहे",
})
_IDENTITY_ECHO_LEFTOVER = frozenset({
    "थी", "था", "थे", "थीं", "thi", "tha", "the",
})

# Devanagari-aware word tokenizer. Python's ``re`` ``\w`` does NOT match
# Devanagari matras (vowel signs U+093A-U+094F) or combining signs (candrabindu
# U+0901, anusvara U+0902), so ``re.findall(r"\w+", "हाँ जी")`` returns the
# base consonants ``{'ह', 'ज'}`` instead of the syllables ``{'हाँ', 'जी'}`` —
# which then fails to intersect ``id_yes_tokens`` like ``हाँ`` / ``जी`` and a
# bare "haan ji" at the identity slot falls through to clarify (DEBT-031).
# Extend ``\w`` with the Devanagari mark/sign ranges (excluding danda U+0964
# and digits U+0966-U+096F, which are punctuation/numbers, not word parts).
_DEVANAGARI_MARK_RANGES = (
    "\u0900-\u0903"  # candrabindu, anusvara, visarga, nukta
    "\u093A-\u094F"  # vowel signs (matras)
    "\u0950-\u0952"  # om, stress signs
    "\u0953-\u0963"  # additional stress signs
)
_WORD_TOKEN_RE = re.compile(
    rf"[\w{_DEVANAGARI_MARK_RANGES}]+",
    re.UNICODE,
)


def _tokenize(transcript: str) -> set[str]:
    """Tokenize a transcript into a set of lowercase word tokens, keeping
    Devanagari syllables (matras + combining signs) intact."""
    return set(_WORD_TOKEN_RE.findall(transcript))


def transcript_blank(transcript: str) -> bool:
    return not (transcript or "").strip()


def is_main_ladder_flow(flow_name: str, profile: TenantRuntimeProfile) -> bool:
    return any(flow_name.startswith(prefix) for prefix in profile.main_ladder_prefixes)


def prune_spurious_objection_stack(
    state: ConversationState,
    profile: TenantRuntimeProfile,
    flows: FlowSet | None = None,
) -> ConversationState:
    """Drop a stale objection frame sitting above the main offer/push ladder.

    Never prune when the objection flow is actively waiting on a collect step
    (e.g. sot_obj_link_request's ``collect: sot_link_received``).  Those frames
    are mid-interaction, not stale.
    """
    if len(state.flow_stack) < 2 or not state.slots.get("identity_ok"):
        return state
    top = state.flow_stack[-1]
    if not top.flow.startswith(profile.objection_prefix):
        return state
    if flows is not None:
        flow_def = flows.flows.get(top.flow)
        if flow_def is not None and top.step_index < len(flow_def.steps):
            if flow_def.steps[top.step_index].collect:
                return state
    if not any(is_main_ladder_flow(frame.flow, profile) for frame in state.flow_stack[:-1]):
        return state
    updated = state.model_copy(deep=True)
    updated.flow_stack = list(state.flow_stack[:-1])
    return updated


def sanitize_blank_transcript_commands(commands: list[Command]) -> list[Command]:
    """Silence/dead-air must not start a flow, clarify-loop, or free-form respond."""
    return [
        c
        for c in commands
        if c.command not in {"start_flow", "clarify", "respond"}
    ]


def dispute_flow(transcript: str, profile: TenantRuntimeProfile) -> str | None:
    """Return the transfer objection flow for a hard dispute, else None."""
    low = (transcript or "").lower()
    loan_tokens = profile.dispute_loan_tokens or ["loan"]
    has_loan = any(tok in low for tok in loan_tokens)
    theme_flows = profile.dispute_theme_flows
    if has_loan and any(d in low for d in profile.cues("dispute_never_loan")):
        return theme_flows.get("never_loan")
    if any(c in low for c in profile.cues("dispute_wrong_amount")):
        return theme_flows.get("wrong_amount")
    if any(c in low for c in profile.cues("dispute_death")):
        return theme_flows.get("death")
    if any(c in low for c in profile.cues("dispute_frozen_account")):
        return theme_flows.get("frozen_account")
    return None


def coerce_dispute(
    commands: list[Command],
    transcript: str,
    *,
    on_rails: bool,
    profile: TenantRuntimeProfile,
) -> tuple[list[Command], bool]:
    if not on_rails:
        return commands, False
    flow = dispute_flow(transcript, profile)
    if flow is None:
        return commands, False
    return [Command(command="start_flow", flow=flow)], True


def coerce_callback_request(
    commands: list[Command],
    transcript: str,
    *,
    on_rails: bool,
    profile: TenantRuntimeProfile,
    scenario: str = "",
) -> tuple[list[Command], bool]:
    """PLO-OOF P1: Tier-1 callback-request deflection.

    Borrower asks to be called back / is busy / has no time now → route
    verbatim to ``profile.callback_flow`` (e.g. plo_obj_callback_pd).
    Fires only on-rails and only when the profile declares a callback_flow
    and a non-empty ``callback_request`` cue pack. Cue match is substring
    on lowercased transcript (same convention as dispute / willing).
    """
    if not on_rails:
        return commands, False
    default = (profile.callback_flow or "").strip()
    if not default:
        return commands, False
    scen = (scenario or "").strip().lower()
    flow = (profile.callback_flow_by_scenario.get(scen) or default).strip()
    low = (transcript or "").lower()
    if not any(cue in low for cue in profile.cues("callback_request")):
        return commands, False
    return [
        Command(command="start_flow", flow=flow),
        Command(
            command="set_slot",
            name="disposition",
            value="callback_request",
            source="system",
        ),
    ], True


def coerce_which_emi(
    commands: list[Command],
    transcript: str,
    *,
    on_rails: bool,
    profile: TenantRuntimeProfile,
    scenario: str = "",
) -> tuple[list[Command], bool]:
    """Catalog which-EMI cue → start_flow. Stub-LLM / cue-miss must not re-ask."""
    if not on_rails:
        return commands, False
    default = (profile.which_emi_flow or "").strip()
    if not default:
        return commands, False
    scen = (scenario or "").strip().lower()
    flow = (profile.which_emi_flow_by_scenario.get(scen) or default).strip()
    low = (transcript or "").lower()
    if not any(cue.lower() in low for cue in profile.cues("which_emi") if cue.strip()):
        return commands, False
    return [Command(command="start_flow", flow=flow)], True


def coerce_push_willing(
    commands: list[Command],
    awaiting_slot: str,
    transcript: str,
    *,
    profile: TenantRuntimeProfile,
) -> tuple[list[Command], bool]:
    if awaiting_slot not in profile.push_intent_slots:
        return commands, False
    existing = next(
        (c for c in commands if c.command == "set_slot" and c.name == awaiting_slot),
        None,
    )
    if existing is not None and str(existing.value or "").lower() in {"willing", "already_paid"}:
        return commands, False
    low = (transcript or "").lower()
    if any(bad in low for bad in profile.cues("willing_disqualifiers")):
        return commands, False
    if not any(cue in low for cue in profile.cues("willing")):
        return commands, False
    kept = [
        c
        for c in commands
        if not (c.command == "set_slot" and c.name == awaiting_slot)
        and c.command != "clarify"
    ]
    kept.append(Command(command="set_slot", name=awaiting_slot, value="willing"))
    return kept, True


_BARE_NEGATION_WRAPPERS = frozenset({"ji", "jee", "जी"})
_BARE_NEGATION_TOKENS = frozenset(
    {"nahi", "nahin", "nhi", "नहीं", "नही", "नहि", "no"}
)


def is_bare_negation(transcript: str, profile: TenantRuntimeProfile) -> bool:
    """Transcript is only a negation token (plus optional जी/ji).

    ``nahi`` / ``नहीं`` at a push-intent slot is a refusal, not unclear.
    Longer lines (``nahi aaj nahi kal``) are not bare and keep existing
    inability / day-shift rules.
    """
    t = (transcript or "").strip()
    if not t:
        return False
    tokens = _tokenize(t.lower())
    if not tokens:
        return False
    neg = set(_BARE_NEGATION_TOKENS)
    for pack in ("negation", "id_no_tokens"):
        for cue in profile.cues(pack):
            neg |= _tokenize((cue or "").lower())
    if not (tokens & neg):
        return False
    return not (tokens - neg - _BARE_NEGATION_WRAPPERS)


def coerce_payment_refusal(
    commands: list[Command],
    awaiting_slot: str,
    transcript: str,
    *,
    profile: TenantRuntimeProfile,
) -> tuple[list[Command], bool, str | None]:
    """Return ``(commands, fired, matched_via, refusal_class)``.

    ``matched_via`` is ``cue``|``regex``|None. ``refusal_class`` is
    ``unwilling`` (will-not) or ``inability`` (cannot) when fired.
    Cue wins when both match (more specific than the shared inability regex).
    """
    if awaiting_slot not in profile.push_intent_slots:
        return commands, False, None, None
    if _DAY_SHIFT_RE.search(transcript or ""):
        return commands, False, None, None
    low = (transcript or "").lower()
    unwilling_cue = any(cue in low for cue in profile.cues("intent_unwilling"))
    unwilling_re = bool(UNWILLINGNESS_RE.search(transcript or ""))
    cue_match = any(cue in low for cue in profile.cues("intent_refusal"))
    regex_match = bool(INABILITY_RE.search(transcript or ""))
    bare = is_bare_negation(transcript, profile)
    if not (cue_match or regex_match or unwilling_cue or unwilling_re or bare):
        return commands, False, None, None
    matched_via = "cue" if (cue_match or unwilling_cue or bare) else "regex"
    refusal_class = (
        "unwilling" if (unwilling_cue or unwilling_re or bare) else "inability"
    )
    existing = next(
        (c for c in commands if c.command == "set_slot" and c.name == awaiting_slot),
        None,
    )
    if existing is not None and str(existing.value or "").strip():
        return commands, False, None, None
    kept = [
        c
        for c in commands
        if not (c.command == "set_slot" and c.name == awaiting_slot)
        and c.command not in {"clarify", "start_flow"}
    ]
    kept.append(
        Command(
            command="set_slot",
            name=awaiting_slot,
            value="refused",
            source="confirmed",
        )
    )
    return kept, True, matched_via, refusal_class


def coerce_identity(
    commands: list[Command],
    awaiting_slot: str,
    transcript: str,
    *,
    profile: TenantRuntimeProfile,
) -> list[Command]:
    slot = profile.identity_slot
    if not slot or awaiting_slot != slot:
        return commands
    if any(c.command == "set_slot" and c.name == slot for c in commands):
        return commands
    low = (transcript or "").strip().lower()
    if not low:
        return commands
    tokens = _tokenize(low)
    if any(p in low for p in profile.cues("id_no_phrases")) or (
        tokens & profile.cue_set("id_no_tokens")
    ):
        return [Command(command="set_slot", name=slot, value="denied")]
    if any(p in low for p in profile.cues("id_yes_phrases")) or (
        tokens & profile.cue_set("id_yes_tokens")
    ):
        return [Command(command="set_slot", name=slot, value="confirmed")]
    return commands


def coerce_consent(
    commands: list[Command],
    awaiting_slot: str,
    transcript: str,
    *,
    profile: TenantRuntimeProfile,
) -> list[Command]:
    """DEBT-043: conversational Hindi affirmatives on plo_consent_2min → yes/no."""
    if awaiting_slot != "plo_consent_2min":
        return commands
    if any(c.command == "set_slot" and c.name == awaiting_slot for c in commands):
        return commands
    low = f" {(transcript or '').strip().lower()} "
    if not low.strip():
        return commands
    if any(cue.lower() in low for cue in profile.cues("consent_no") if cue.strip()):
        return [Command(command="set_slot", name=awaiting_slot, value="no")]
    if any(cue.lower() in low for cue in profile.cues("consent_yes") if cue.strip()):
        return [Command(command="set_slot", name=awaiting_slot, value="yes")]
    return commands


def coerce_commit_reversal(
    commands: list[Command],
    awaiting_slot: str,
    transcript: str,
    *,
    profile: TenantRuntimeProfile,
) -> tuple[list[Command], bool]:
    if awaiting_slot not in profile.reversal_slots:
        return commands, False
    low = (transcript or "").lower()
    # DEBT-016: prefer a dedicated reversal cue pack when configured (PLO),
    # fall back to the general refusal pack (SOT preserves prior behaviour).
    reversal_cues = profile.cues("reversal")
    cue_pack = reversal_cues if reversal_cues else profile.cues("refusal")
    if not any(cue in low for cue in cue_pack):
        return commands, False
    supplied_time = any(
        c.command == "set_slot"
        and c.name in profile.timing_slot_set
        and str(c.value or "").strip()
        and str(c.value).strip().lower() not in {"unwilling", "no", "none", "unknown"}
        for c in commands
    )
    if supplied_time:
        return commands, False
    target = profile.reversal_target_flow
    if not target:
        return commands, False
    out: list[Command] = [Command(command="start_flow", flow=target)]
    # DEBT-016 (H3 reversal): clear committed_date on fire when it is one of
    # the reversal slots (PLO). SOT's reversal_slots don't include it, so SOT
    # behaviour is unchanged.
    if "committed_date" in profile.reversal_slots:
        out.append(Command(command="set_slot", name="committed_date", value=""))
    return out, True


def coerce_confirm(
    commands: list[Command],
    awaiting_slot: str,
    transcript: str,
    *,
    profile: TenantRuntimeProfile,
) -> list[Command]:
    slot = profile.final_confirm_slot
    if not slot or awaiting_slot != slot:
        return commands
    if any(c.command == "set_slot" and c.name == slot for c in commands):
        return commands
    timing_slots = set(profile.timing_slot_set)
    restated = any(
        c.command == "set_slot" and c.name in timing_slots for c in commands
    )
    if not restated:
        return commands
    low = (transcript or "").lower()
    value = "no" if any(cue in low for cue in profile.cues("negation")) else "yes"
    return [Command(command="set_slot", name=slot, value=value)]


def coerce_link_received(
    commands: list[Command],
    awaiting_slot: str,
    transcript: str,
    *,
    profile: TenantRuntimeProfile,
) -> list[Command]:
    slot = profile.link_received_slot
    if not slot or awaiting_slot != slot:
        return commands
    low = (transcript or "").strip().lower()
    value = (
        "not_received"
        if any(c in low for c in profile.cues("link_not_received"))
        else "received"
    )
    commands = [
        c for c in commands if not (c.command == "set_slot" and c.name == slot)
    ]
    return [*commands, Command(command="set_slot", name=slot, value=value)]


_REASON_CATCHALL_MAX = 140
_IST = ZoneInfo("Asia/Kolkata")
_MAX_PTP_DAYS = 30
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
_WORD_NUMBERS: dict[str, int] = {
    "ek": 1, "एक": 1, "do": 2, "दो": 2, "teen": 3, "तीन": 3,
    "char": 4, "chaar": 4, "चार": 4, "paanch": 5, "panch": 5, "पाँच": 5, "पांच": 5,
    "chhe": 6, "che": 6, "छह": 6, "saat": 7, "सात": 7, "aath": 8, "आठ": 8,
    "nau": 9, "नौ": 9, "das": 10, "दस": 10, "gyaarah": 11, "gyarah": 11, "ग्यारह": 11,
    "baarah": 12, "barah": 12, "बारह": 12, "terah": 13, "तेरह": 13,
    "chaudah": 14, "चौदह": 14, "pandrah": 15, "पंद्रह": 15, "पन्द्रह": 15,
    "solah": 16, "सोलह": 16, "satrah": 17, "सत्रह": 17, "athaarah": 18, "अठारह": 18,
    "unnis": 19, "उन्नीस": 19, "bees": 20, "बीस": 20, "ikkis": 21, "इक्कीस": 21,
    "bais": 22, "बाईस": 22, "teis": 23, "तेईस": 23, "chaubis": 24, "चौबीस": 24,
    "pachis": 25, "पच्चीस": 25, "chabbis": 26, "छब्बीस": 26, "sattais": 27, "सत्ताईस": 27,
    "athais": 28, "अट्ठाईस": 28, "untis": 29, "उनतीस": 29, "tees": 30, "तीस": 30,
}
_VAGUE_LATER_RE = re.compile(
    r"(बाद\s*में|बाद\s*मे|\bbaad\s*me(?:in)?\b|जल्द\s*ही|जल्दी(?:\s*ही)?"
    r"|\bjald(?:i)?(?:\s*hi)?\b|\bjaldi\b)",
    re.IGNORECASE,
)
_REL_N_DAYS_RE = re.compile(
    r"(?P<n>\d+|[a-zA-Z\u0900-\u097F]+)\s*"
    r"(?:din|दिन|day|days)\s*"
    r"(?:baad|बाद|mein|में|me\b)",
    re.IGNORECASE,
)
_KAL_RE = re.compile(r"(?<!\w)(kal|कल)(?!\w)", re.IGNORECASE)
_PARSO_RE = re.compile(
    r"(?<!\w)(parso[n]?|parason|परसों|परसो)(?!\w)", re.IGNORECASE
)
_NEXT_WEEK_RE = re.compile(
    r"(agle\s+haft[ae]|next\s+week|अगले\s+हफ़?्?ते|अगले\s+सप्ताह)",
    re.IGNORECASE,
)
_NEXT_MONTH_RE = re.compile(
    r"(agle\s+mahin[ae]|next\s+month|अगले\s+मह[ीि]ने)",
    re.IGNORECASE,
)


def today_ist(call_date: str | date | None = None) -> date:
    """Asia/Kolkata today; ``call_date`` pins replay/tests."""
    if isinstance(call_date, date):
        return call_date
    if isinstance(call_date, str) and len(call_date) >= 10:
        try:
            return date.fromisoformat(call_date[:10])
        except ValueError:
            pass
    return datetime.now(_IST).date()


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, last))


def _parse_day_count(raw: str) -> int | None:
    text = (raw or "").strip().translate(_DEVANAGARI_DIGITS).lower()
    if text.isdigit():
        n = int(text)
        return n if n > 0 else None
    return _WORD_NUMBERS.get(text) or _WORD_NUMBERS.get(raw.strip())


def is_vague_later(transcript: str) -> bool:
    """True for 'baad mein / jald hi' with no concrete relative/absolute date."""
    if not transcript or _extract_committed_date(transcript):
        return False
    return bool(_VAGUE_LATER_RE.search(transcript))


def _extract_committed_date(transcript: str, *, today: date | None = None) -> str | None:
    """G-B4-02: extract a borrower-committed date from a free-text timeline.

    Returns an ISO ``YYYY-MM-DD`` string, or ``None``. Handles ISO dates,
    ``dd/mm/yyyy`` / ``dd-mm-yyyy``, English month names, and Devanagari month
    names. Year defaults to the current year when omitted.
    """
    if not transcript:
        return None
    today = today or today_ist()
    text = transcript.translate(_DEVANAGARI_DIGITS)

    # ISO yyyy-mm-dd
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except ValueError:
            pass

    # dd/mm/yyyy or dd-mm-yyyy
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", text)
    if m:
        d, mo, y = int(m[1]), int(m[2]), int(m[3])
        if y < 100:
            y += 2000
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            pass

    # English month names (full + abbreviations).
    en_months = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
        "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
        "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    # Devanagari month names.
    hi_months = {
        "जनवरी": 1, "फरवरी": 2, "मार्च": 3, "अप्रैल": 4, "मई": 5, "जून": 6,
        "जुलाई": 7, "अगस्त": 8, "अगस्ट": 8, "सितंबर": 9, "सितम्बर": 9, "अक्टूबर": 10,
        "अक्टूबर": 10, "नवंबर": 11, "नवम्बर": 11, "दिसंबर": 12, "दिसम्बर": 12,
    }
    low = text.lower()
    month_num = 0
    for name, num in {**en_months, **hi_months}.items():
        if name in low or name in text:
            month_num = num
            break
    if month_num:
        m = re.search(r"\b(\d{1,2})\b", text)
        if m:
            d = int(m[1])
            ym = re.search(r"\b(\d{4})\b", text)
            y = int(ym[1]) if ym else today.year
            try:
                return date(y, month_num, d).isoformat()
            except ValueError:
                pass

    # Relative: N din baad / kal / parso / agle hafte / agle mahine.
    rel = _REL_N_DAYS_RE.search(text)
    if rel:
        n = _parse_day_count(rel.group("n"))
        if n:
            return (today + timedelta(days=n)).isoformat()
    if _NEXT_WEEK_RE.search(text):
        return (today + timedelta(days=7)).isoformat()
    if _NEXT_MONTH_RE.search(text):
        return _add_months(today, 1).isoformat()
    if _PARSO_RE.search(text):
        return (today + timedelta(days=2)).isoformat()
    if _KAL_RE.search(text):
        return (today + timedelta(days=1)).isoformat()
    return None


def coerce_committed_date(
    commands: list[Command],
    awaiting_slot: str,
    transcript: str,
    *,
    profile: TenantRuntimeProfile,
) -> tuple[list[Command], bool]:
    """G-B4-02: write committed_date back when the borrower names a date.

    Fires when awaiting ``profile.reason_slot`` (PaisaLo timeline) and the
    transcript contains a parseable date. Sets ``committed_date`` (ISO) and
    routes the timeline to ``specific_date`` so the assurance-date reply can
    speak the committed date. Runs before ``coerce_reason_catchall`` so the
    raw-text catchall does not swallow a real date.
    """
    slot = (profile.reason_slot or "").strip()
    if not slot or awaiting_slot != slot:
        return commands, False
    if transcript_blank(transcript):
        return commands, False
    iso = _extract_committed_date(transcript, today=today_ist())
    if not iso:
        return commands, False
    kept = [
        c
        for c in commands
        if not (c.command == "set_slot" and c.name in {slot, "committed_date"})
        and c.command != "clarify"
    ]
    kept.append(Command(command="set_slot", name="committed_date", value=iso))
    kept.append(Command(command="set_slot", name=slot, value="specific_date"))
    return kept, True


def coerce_intent_date(
    commands: list[Command],
    awaiting_slot: str,
    transcript: str,
    *,
    profile: TenantRuntimeProfile,
    today: date | None = None,
) -> tuple[list[Command], bool, str | None]:
    """L3-FIX P2: relative / vague date at the push-intent slot (PaisaLo ON).

    Concrete (kal / parso / N din baad / calendar) → committed_date + willing
    (+ plo_timeline=specific_date when that slot exists on the profile).
    Date > max_ptp_days: if tenant has ptp_policy (W3-1), write the date
    so the confirm seam can counter; otherwise no write (L3-FIX nearer-ask).
    Past dates → no date write; caller asks nearer.
    Vague (baad mein / jald hi) → no date; caller asks for one concrete day.
    """
    if not getattr(profile, "supports_intent_date_coercion", False):
        return commands, False, None
    if awaiting_slot not in profile.push_intent_slots:
        return commands, False, None
    if transcript_blank(transcript):
        return commands, False, None
    today = today or today_ist()
    iso = _extract_committed_date(transcript, today=today)
    drop = {awaiting_slot, "committed_date", (profile.reason_slot or "").strip()}
    drop.discard("")
    kept = [
        c
        for c in commands
        if not (c.command == "set_slot" and c.name in drop)
        and c.command != "clarify"
    ]
    if iso:
        parsed = date.fromisoformat(iso)
        delta = (parsed - today).days
        raw_policy = getattr(profile, "ptp_policy", None) or {}
        try:
            max_days = int(raw_policy.get("max_ptp_days", _MAX_PTP_DAYS))
        except (TypeError, ValueError, AttributeError):
            max_days = _MAX_PTP_DAYS
        has_policy = bool(raw_policy)
        if delta < 0:
            return kept, True, "nearer"
        if delta > max_days and not has_policy:
            return kept, True, "nearer"
        kept.append(Command(command="set_slot", name="committed_date", value=iso))
        kept.append(Command(command="set_slot", name=awaiting_slot, value="willing"))
        reason = (profile.reason_slot or "").strip()
        if reason:
            kept.append(Command(command="set_slot", name=reason, value="specific_date"))
        return kept, True, None
    if is_vague_later(transcript):
        return kept, True, "concrete"
    return commands, False, None


def coerce_reason_catchall(
    commands: list[Command],
    awaiting_slot: str,
    transcript: str,
    *,
    profile: TenantRuntimeProfile,
) -> tuple[list[Command], bool]:
    """LAST-chain free-text reason fill when LLM omitted a valid set_slot.

    Fires only when awaiting ``profile.reason_slot``, transcript is non-blank,
    and commands contain no set_slot for that slot (empty/rejected LLM path).
    """
    slot = (profile.reason_slot or "").strip()
    if not slot or awaiting_slot != slot:
        return commands, False
    if transcript_blank(transcript):
        return commands, False
    for c in commands:
        if (
            c.command == "set_slot"
            and c.name == slot
            and (c.value or "").strip()
        ):
            return commands, False
    value = (transcript or "").strip()[:_REASON_CATCHALL_MAX]
    kept = [
        c
        for c in commands
        if not (c.command == "set_slot" and c.name == slot) and c.command != "clarify"
    ]
    kept.append(Command(command="set_slot", name=slot, value=value))
    return kept, True


def _identity_yes_skip(
    transcript: str,
    profile: TenantRuntimeProfile,
    borrower_name: str = "",
) -> bool:
    """F3: identity D1 skip = bare yes-token or yes+name only.

    Bot-utterance leftovers (थी/था from "बोल रही थी") never skip.
    """
    low = (transcript or "").strip().lower()
    if not low:
        return False
    tokens = _tokenize(low)
    yes_tokens = profile.cue_set("id_yes_tokens")
    has_yes = bool(tokens & yes_tokens) or any(
        p in low for p in profile.cues("id_yes_phrases")
    )
    if not has_yes:
        return False
    if tokens & _IDENTITY_ECHO_LEFTOVER:
        return False
    extra = tokens - yes_tokens - _IDENTITY_FILLERS
    extra -= _tokenize((borrower_name or "").lower())
    return len(extra) <= 1


def run_coercion_chain(
    commands: list[Command],
    awaiting_slot: str,
    transcript: str,
    *,
    profile: TenantRuntimeProfile,
    on_rails: bool,
    blank_transcript: bool,
    pending_confirm: dict | None = None,
    today: date | None = None,
    scenario: str = "",
) -> tuple[list[Command], dict[str, str | None]]:
    """Execute the scripted coercion chain with existing short-circuit semantics.

    Order (documented in ``profile.coercion_chain``):
    dispute → callback → which_emi → willing → refusal → {identity, reversal,
    [confirm], link} → reason_catchall (LAST; only when no earlier short-circuit
    fired, including callback). Link still runs when reversal fires; identity
    never short-circuits siblings.

    Returns ``(commands, meta)`` where meta may include ``refusal_matched_via``.
    """
    meta: dict[str, str | None] = {"refusal_matched_via": None, "refusal_class": None}
    today = today or today_ist()
    if blank_transcript:
        commands = sanitize_blank_transcript_commands(commands)

    # F5: pending_confirm(v) + same-v / yes cue → replay the locked value
    # so evidence 3 has a candidate write to execute (e1d5d837 t7 "नहीं।").
    if isinstance(pending_confirm, dict):
        from app.engine.evidence_scorer import confirms_pending_value

        p_slot = str(pending_confirm.get("slot") or "").strip()
        p_value = pending_confirm.get("value")
        p_date = pending_confirm.get("committed_date")
        if (
            p_slot
            and p_value not in (None, "")
            and confirms_pending_value(
                transcript, profile, str(p_value),
                pending_date=str(p_date) if p_date else None,
                today=today,
            )
        ):
            if not any(
                c.command == "set_slot" and c.name == p_slot and str(c.value or "").strip()
                for c in commands
            ):
                commands = [c for c in commands if c.command != "clarify"]
                commands.append(
                    Command(command="set_slot", name=p_slot, value=p_value)
                )
                meta["pending_confirm_replay"] = str(p_value)
            if p_date and not any(
                c.command == "set_slot" and c.name == "committed_date" and str(c.value or "").strip()
                for c in commands
            ):
                commands.append(
                    Command(command="set_slot", name="committed_date", value=str(p_date))
                )
                reason = (profile.reason_slot or "").strip()
                if reason and not any(
                    c.command == "set_slot" and c.name == reason for c in commands
                ):
                    commands.append(
                        Command(command="set_slot", name=reason, value="specific_date")
                    )

    commands, dispute_fired = coerce_dispute(
        commands, transcript, on_rails=on_rails, profile=profile
    )
    callback_fired = False
    if not dispute_fired:
        commands, callback_fired = coerce_callback_request(
            commands, transcript, on_rails=on_rails, profile=profile, scenario=scenario
        )
    which_emi_fired = False
    if not dispute_fired and not callback_fired:
        commands, which_emi_fired = coerce_which_emi(
            commands,
            transcript,
            on_rails=on_rails,
            profile=profile,
            scenario=scenario,
        )
    willing_fired = False
    refusal_fired = False
    date_fired = False
    if not dispute_fired and not callback_fired and not which_emi_fired:
        commands, date_fired, date_ask = coerce_intent_date(
            commands, awaiting_slot, transcript, profile=profile, today=today
        )
        if date_ask:
            meta["date_ask"] = date_ask
        if date_fired and not date_ask:
            meta["intent_date"] = "concrete"
    if not dispute_fired and not callback_fired and not which_emi_fired and not date_fired:
        commands, willing_fired = coerce_push_willing(
            commands, awaiting_slot, transcript, profile=profile
        )
    if (
        not dispute_fired
        and not callback_fired
        and not which_emi_fired
        and not willing_fired
        and not date_fired
    ):
        commands, refusal_fired, refusal_via, refusal_class = coerce_payment_refusal(
            commands, awaiting_slot, transcript, profile=profile
        )
        if refusal_fired:
            meta["refusal_matched_via"] = refusal_via
            meta["refusal_class"] = refusal_class
    if (
        not dispute_fired
        and not callback_fired
        and not which_emi_fired
        and not willing_fired
        and not refusal_fired
        and not date_fired
    ):
        commands = coerce_identity(
            commands, awaiting_slot, transcript, profile=profile
        )
        commands = coerce_consent(
            commands, awaiting_slot, transcript, profile=profile
        )
        commands, reversal_fired = coerce_commit_reversal(
            commands, awaiting_slot, transcript, profile=profile
        )
        if not reversal_fired:
            commands = coerce_confirm(
                commands, awaiting_slot, transcript, profile=profile
            )
        commands = coerce_link_received(
            commands, awaiting_slot, transcript, profile=profile
        )
        # G-B4-02: capture committed_date BEFORE the raw-text catchall swallows
        # a real date; routes the timeline to specific_date so the assurance-date
        # reply can speak the committed date.
        commands, timeline_date_fired = coerce_committed_date(
            commands, awaiting_slot, transcript, profile=profile
        )
        # LAST: free-text reason catchall (SOT payment_problem / PaisaLo timeline).
        if not timeline_date_fired:
            commands, _ = coerce_reason_catchall(
                commands, awaiting_slot, transcript, profile=profile
            )
    return commands, meta


def cue_hit_pack(
    transcript: str,
    awaiting_slot: str,
    *,
    profile: TenantRuntimeProfile,
    on_rails: bool,
    borrower_name: str = "",
    pending_confirm: dict | None = None,
) -> str | None:
    """D1: return the cue pack that would fully route this turn, or None.

    Question-shaped transcripts never skip (E3 mixed utterances like
    "haan. office kahan hai?" must still reach command_gen).
    Identity skip is bare yes-token or yes+name only (F3).
    """
    from app.engine.evidence_scorer import confirms_pending_value, has_question_shape

    if not (transcript or "").strip():
        return None
    low = (transcript or "").lower()
    if has_question_shape(transcript):
        if (
            on_rails
            and (profile.which_emi_flow or "").strip()
            and any(cue.lower() in low for cue in profile.cues("which_emi") if cue.strip())
            and not any(cue in low for cue in profile.cues("willing"))
        ):
            return "which_emi"
        return None

    if isinstance(pending_confirm, dict) and pending_confirm.get("value") not in (None, ""):
        p_date = pending_confirm.get("committed_date")
        if confirms_pending_value(
            transcript, profile, str(pending_confirm.get("value")),
            pending_date=str(p_date) if p_date else None,
        ):
            return "pending_confirm"

    if on_rails and dispute_flow(transcript, profile):
        return "dispute"
    if (
        on_rails
        and (profile.callback_flow or "").strip()
        and any(cue in transcript.lower() for cue in profile.cues("callback_request"))
    ):
        return "callback"
    if awaiting_slot in profile.push_intent_slots:
        low = transcript.lower()
        if not any(bad in low for bad in profile.cues("willing_disqualifiers")):
            if any(cue in low for cue in profile.cues("willing")):
                return "willing"
        if not _DAY_SHIFT_RE.search(transcript or "") and (
            is_bare_negation(transcript, profile)
            or any(cue in low for cue in profile.cues("intent_refusal"))
            or any(cue in low for cue in profile.cues("intent_unwilling"))
            or INABILITY_RE.search(transcript or "")
            or UNWILLINGNESS_RE.search(transcript or "")
        ):
            return "refusal"
    slot = profile.identity_slot
    if slot and awaiting_slot == slot:
        low = transcript.strip().lower()
        tokens = _tokenize(low)
        if any(p in low for p in profile.cues("id_no_phrases")) or (
            tokens & profile.cue_set("id_no_tokens")
        ):
            return "identity"
        if _identity_yes_skip(transcript, profile, borrower_name=borrower_name):
            return "identity"
    if (
        on_rails
        and (profile.which_emi_flow or "").strip()
        and any(cue.lower() in low for cue in profile.cues("which_emi") if cue.strip())
    ):
        return "which_emi"
    return None
