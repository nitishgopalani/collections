"""W2-1 Evidence scorer (0-3) — TELEMETRY-ONLY this phase.

Score rubric (per W2_SPRINT_SPEC.md §W2-1):
  0 = echo / backchannel token (tenant YAML list) / non-addressed
  1 = LLM-only (no cue-pack match, no borrower-repeat)
  2 = LLM + cue agree OR borrower repeated a prior utterance
  3 = explicitly confirmed the previous turn (yes-phrase at a confirm / identity slot)

Logged per turn as ``evidence`` + ``evidence_reason`` in the turn_decision
guards dict. The Commitment Gate (W2-2) consumes this score; W2-1 does NOT
change behaviour — the score is computed and logged only.

Devanagari-aware: reuses ``_tokenize`` + ``normalize``. Backchannel tokens and
cue packs come from the ``TenantRuntimeProfile`` (``backchannel_tokens`` list +
``cue_packs``). The "borrower repeated" signal compares the current transcript
to ``state.slots["_last_borrower_transcript"]`` (written at the end of each
non-echo turn).
"""

from __future__ import annotations

from typing import Any

from app.engine.compliance_rules import normalize
from datetime import date

from app.engine.scripted_coercions import (
    INABILITY_RE,
    UNWILLINGNESS_RE,
    _extract_committed_date,
    _tokenize,
    today_ist,
)
from app.schemas.command import Command
from app.schemas.state import ConversationState

# Slots / confirm-slot families used by the explicit-confirm detector.
_CONFIRM_SLOT_MARKERS = ("confirm", "identity")
_LAST_BORROWER_KEY = "_last_borrower_transcript"

# E3: question-shape markers. A yes-token PLUS one of these in the same
# transcript is a question, not an explicit confirm (live dc4c5808 t4:
# "हाँ। ऑफिस कहाँ है?").
_QUESTION_MARKERS = (
    "कहाँ", "कहां", "क्या", "कौन", "क्यों", "कैसे", "कब", "किस",
    "kahan", "kahaan", "kya", "kaun", "kyun", "kaise", "kab", "kis",
    "where", "what", "which", "why", "how", "when",
    "?",
)


def has_question_shape(transcript: str) -> bool:
    """True when the transcript contains a question marker (Devanagari /
    Roman / '?'). Used by the evidence scorer (E3) and the turn path
    (answer-first: strip money-state writes, keep pending_confirm)."""
    t = (transcript or "").strip()
    if not t:
        return False
    low = t.lower()
    for m in _QUESTION_MARKERS:
        if m in t or m in low:
            return True
    return False


def _is_backchannel(transcript: str, backchannel_tokens: list[str]) -> bool:
    """Transcript is (almost) entirely backchannel acknowledgment tokens."""
    if not backchannel_tokens:
        return False
    t = (transcript or "").strip()
    if not t:
        return False
    tokens = _tokenize(t.lower())
    if not tokens:
        return False
    bc_word_tokens: set[str] = set()
    for b in backchannel_tokens:
        bc_word_tokens |= _tokenize(b.lower())
    if not bc_word_tokens:
        return False
    non_bc = tokens - bc_word_tokens
    # Pure backchannel: at most one non-backchannel token AND at least one
    # backchannel token present (so "haan ji" with no extra word counts, but
    # "haan, main ramesh" does not — that's a real answer).
    return len(non_bc) <= 1 and bool(tokens & bc_word_tokens)


def _borrower_repeated(transcript: str, state: ConversationState) -> bool:
    """Borrower repeated a prior utterance verbatim (frustration / stall)."""
    t_norm = normalize(transcript)
    if not t_norm or len(t_norm) < 4:
        return False
    prev = state.slots.get(_LAST_BORROWER_KEY)
    if isinstance(prev, str) and prev:
        return normalize(prev) == t_norm
    return False


def _tokenize_list(text: str) -> list[str]:
    """Ordered token list (``_tokenize`` returns a set — use this for phrase
    subsequence matching where order matters)."""
    from app.engine.scripted_coercions import _WORD_TOKEN_RE

    return [w for w in _WORD_TOKEN_RE.findall((text or "").lower()) if w]


def _cue_agree(transcript: str, profile: Any) -> bool:
    """Transcript matches a cue-pack for the active collect slot.

    Token-level (word-boundary) match — NOT substring — so a short cue like
    "han" does not match inside "change", and "haan" does not match inside
    "kahaan". Cues that are multi-word phrases (e.g. "haan ji") are matched
    as a contiguous token subsequence against the transcript token list.
    """
    if profile is None:
        return False
    t = (transcript or "").strip()
    if not t:
        return False
    cues_fn = getattr(profile, "cues", None)
    if not callable(cues_fn):
        return False
    t_tokens = _tokenize(t.lower())
    if not t_tokens:
        return False
    t_token_list = _tokenize_list(t)
    for pack in ("willing", "id_yes_phrases", "id_yes_tokens"):
        if _pack_tokens_match(pack, cues_fn, t_tokens, t_token_list):
            return True
    return False


def _cue_refuse(transcript: str, profile: Any) -> bool:
    """Transcript matches a negation / refusal cue-pack (not agreement).

    Bare ``nahi`` / ``नहीं`` used to score ``cue_agree`` because ``id_no_*``
    lived in ``_cue_agree``. That hid a payment-intent refusal as ev2 agree
    and burned the repair ladder (session db3037ad01ef).
    """
    if profile is None:
        return False
    t = (transcript or "").strip()
    if not t:
        return False
    cues_fn = getattr(profile, "cues", None)
    if not callable(cues_fn):
        return False
    t_tokens = _tokenize(t.lower())
    if not t_tokens:
        return False
    t_token_list = _tokenize_list(t)
    for pack in (
        "negation",
        "id_no_phrases",
        "id_no_tokens",
        "intent_refusal",
        "intent_unwilling",
    ):
        if _pack_tokens_match(pack, cues_fn, t_tokens, t_token_list):
            return True
    return False


def _pack_tokens_match(
    pack: str,
    cues_fn: Any,
    t_tokens: set[str],
    t_token_list: list[str],
) -> bool:
    for c in cues_fn(pack):
        if not c:
            continue
        c_token_set = _tokenize(c.lower())
        if not c_token_set:
            continue
        if len(c_token_set) == 1:
            if t_tokens & c_token_set:
                return True
            continue
        c_token_list = _tokenize_list(c)
        c_len = len(c_token_list)
        for i in range(len(t_token_list) - c_len + 1):
            if t_token_list[i : i + c_len] == c_token_list:
                return True
    return False


_REFUSED_VALUES = frozenset({"refused", "unwilling", "later", "denied", "no"})
_WILLING_VALUES = frozenset({"willing", "confirmed", "yes", "haan"})


def confirms_pending_value(
    transcript: str,
    profile: Any,
    pending_value: str,
    *,
    pending_date: str | None = None,
    today: date | None = None,
) -> bool:
    """F5 / L3-FIX P4: pending_confirm(v) + yes-token OR restated same v.

    Refused pending: nahi / refusal / unwilling / inability confirms it.
    Willing pending: yes-token / willing cue confirms it (nahi does not).
    Date pending: same resolved date (relative or calendar) confirms it.
    """
    if profile is None or not (transcript or "").strip():
        return False
    t = transcript.strip()
    if has_question_shape(t):
        return False
    v = str(pending_value or "").strip().lower()
    iso = (pending_date or "").strip()
    if not iso and len(v) == 10 and v[4] == "-" and v[7] == "-":
        iso = v
    if iso:
        extracted = _extract_committed_date(t, today=today or today_ist())
        if extracted == iso:
            return True
    low = t.lower()
    tokens = _tokenize(low)
    cues_fn = getattr(profile, "cues", None)
    cue_set_fn = getattr(profile, "cue_set", None)
    if not callable(cues_fn) or not callable(cue_set_fn):
        return False
    yes_tokens = {normalize(x) for x in cue_set_fn("id_yes_tokens") if x}
    has_yes = bool(tokens & yes_tokens) or any(
        p and p in low for p in cues_fn("id_yes_phrases")
    )
    has_no = bool(tokens & {normalize(x) for x in cue_set_fn("id_no_tokens") if x}) or any(
        p and p in low for p in cues_fn("id_no_phrases")
    )
    has_refusal = (
        any(c and c in low for c in cues_fn("intent_refusal"))
        or any(c and c in low for c in cues_fn("intent_unwilling"))
        or bool(INABILITY_RE.search(t))
        or bool(UNWILLINGNESS_RE.search(t))
    )
    has_willing = any(c and c in low for c in cues_fn("willing"))
    if v in _REFUSED_VALUES:
        return bool(has_yes or has_no or has_refusal)
    if v in _WILLING_VALUES:
        if has_no and not has_yes:
            return False
        return bool(has_yes or has_willing)
    return bool(has_yes)


def _explicit_confirm(
    transcript: str,
    profile: Any,
    awaited_slot: str | None,
    *,
    pending_confirm: bool = False,
    pending_value: str | None = None,
    pending_date: str | None = None,
    today: date | None = None,
) -> bool:
    """Explicitly confirmed the previous turn — yes-phrase at a confirm /
    identity slot, OR a yes-token when the gate issued a confirm-ask last
    turn (``_pending_confirm`` set). The latter is the enforce-mode path:
    the gate downgraded a money-state write to a confirm-ask, and the
    borrower's bare "haan" / "haan pakka" IS the explicit confirm."""
    if profile is None:
        return False
    slot_low = (awaited_slot or "").lower()
    is_confirm_slot = any(marker in slot_low for marker in _CONFIRM_SLOT_MARKERS)
    if not is_confirm_slot and not pending_confirm:
        return False
    t = (transcript or "").strip()
    if not t:
        return False
    # E3: pending_confirm + yes-token + question-markers in the SAME
    # transcript is NOT an explicit confirm — the borrower is asking a
    # question (possibly after a backchannel हाँ). Route the question;
    # keep pending_confirm armed. Bare "haan" / "haan pakka" still score 3.
    if pending_confirm and has_question_shape(t):
        return False
    # F5: pending_confirm(v) + repeated same-v cue = evidence 3.
    if pending_confirm and pending_value not in (None, ""):
        if confirms_pending_value(
            t, profile, str(pending_value),
            pending_date=pending_date, today=today,
        ):
            return True
    cues_fn = getattr(profile, "cues", None)
    cue_set_fn = getattr(profile, "cue_set", None)
    if not callable(cues_fn) or not callable(cue_set_fn):
        return False
    t_tokens = _tokenize(t.lower())
    if not t_tokens:
        return False
    t_token_list = _tokenize_list(t)
    for p in cues_fn("id_yes_phrases"):
        if not p:
            continue
        p_tokens = _tokenize_list(p)
        if not p_tokens:
            continue
        p_len = len(p_tokens)
        for i in range(len(t_token_list) - p_len + 1):
            if t_token_list[i : i + p_len] == p_tokens:
                return True
    yes_tokens = {normalize(t) for t in cue_set_fn("id_yes_tokens") if t}
    if t_tokens & yes_tokens:
        return True
    return False


def score_evidence(
    *,
    transcript: str,
    state: ConversationState,
    profile: Any,
    llm_calls: int,
    commands: list[Command],
    last_spoken_reply: str,
    echo: bool,
    awaited_slot: str | None,
    pending_confirm: bool = False,
    pending_value: str | None = None,
    pending_date: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Return ``{evidence, evidence_reason, evidence_signals}`` for the turn.

    Pure function — no state mutation. Caller logs it in guards.
    """
    backchannel: list[str] = list(
        getattr(profile, "backchannel_tokens", None) or []
    ) if profile is not None else []

    if echo:
        return {"evidence": 0, "evidence_reason": "echo", "evidence_signals": {}}

    # Explicit confirm (score 3) is checked BEFORE backchannel so a bare
    # "haan" / "हाँ" at a confirm / identity slot (OR when the gate issued a
    # confirm-ask last turn via _pending_confirm) scores 3 (explicit confirm),
    # not 0 (backchannel) — the borrower IS answering, not just nodding.
    if _explicit_confirm(
        transcript, profile, awaited_slot,
        pending_confirm=pending_confirm, pending_value=pending_value,
        pending_date=pending_date, today=today,
    ):
        return {
            "evidence": 3,
            "evidence_reason": "explicit_confirm",
            "evidence_signals": {
                "slot": awaited_slot,
                "pending_confirm": pending_confirm,
                "pending_value": pending_value,
            },
        }

    if _is_backchannel(transcript, backchannel):
        return {
            "evidence": 0,
            "evidence_reason": "backchannel",
            "evidence_signals": {"matched": backchannel[:5]},
        }

    if not (transcript or "").strip():
        return {
            "evidence": 0,
            "evidence_reason": "non_addressed_blank",
            "evidence_signals": {},
        }

    refuse = _cue_refuse(transcript, profile)
    cue = _cue_agree(transcript, profile)
    repeated = _borrower_repeated(transcript, state)
    if refuse:
        return {
            "evidence": 2,
            "evidence_reason": "cue_refuse",
            "evidence_signals": {"cue": False, "refuse": True, "repeated": repeated},
        }
    if cue or repeated:
        return {
            "evidence": 2,
            "evidence_reason": "cue_agree" if cue else "borrower_repeated",
            "evidence_signals": {"cue": cue, "repeated": repeated},
        }

    if llm_calls >= 1:
        return {"evidence": 1, "evidence_reason": "llm_only", "evidence_signals": {}}

    return {"evidence": 0, "evidence_reason": "non_addressed", "evidence_signals": {}}
