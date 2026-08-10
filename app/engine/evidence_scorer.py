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
from app.engine.scripted_coercions import _tokenize
from app.schemas.command import Command
from app.schemas.state import ConversationState

# Slots / confirm-slot families used by the explicit-confirm detector.
_CONFIRM_SLOT_MARKERS = ("confirm", "identity")
_LAST_BORROWER_KEY = "_last_borrower_transcript"


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
    for pack in ("willing", "id_yes_phrases", "id_no_phrases", "id_yes_tokens", "id_no_tokens"):
        for c in cues_fn(pack):
            if not c:
                continue
            c_token_set = _tokenize(c.lower())
            if not c_token_set:
                continue
            # Single-token cue: word-boundary membership (set intersection).
            if len(c_token_set) == 1:
                if t_tokens & c_token_set:
                    return True
                continue
            # Multi-token phrase: contiguous subsequence in the ordered token list.
            c_token_list = _tokenize_list(c)
            c_len = len(c_token_list)
            for i in range(len(t_token_list) - c_len + 1):
                if t_token_list[i : i + c_len] == c_token_list:
                    return True
    return False


def _explicit_confirm(transcript: str, profile: Any, awaited_slot: str | None) -> bool:
    """Explicitly confirmed the previous turn — yes-phrase at a confirm / identity slot."""
    if profile is None or not awaited_slot:
        return False
    slot_low = awaited_slot.lower()
    if not any(marker in slot_low for marker in _CONFIRM_SLOT_MARKERS):
        return False
    t = (transcript or "").strip()
    if not t:
        return False
    cues_fn = getattr(profile, "cues", None)
    cue_set_fn = getattr(profile, "cue_set", None)
    if not callable(cues_fn) or not callable(cue_set_fn):
        return False
    t_tokens = _tokenize(t.lower())
    if not t_tokens:
        return False
    t_token_list = _tokenize_list(t)
    # Phrase match (contiguous token subsequence) — word-boundary safe.
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
    # Bare yes-token at a confirm / identity slot (haan / हाँ / bilkul / sahi).
    # At a confirm slot a yes-token ANYWHERE in the transcript is an explicit
    # confirm — even in a full sentence like "हाँ, मैं रमेश बोल रहा हूँ।" (6 tokens).
    # Score 2 (cue_agree) is for collect slots; score 3 is for confirm/identity
    # slots, so we do not length-restrict here. Only YES tokens count — a
    # denial ("नहीं, मैं रमेश नहीं हूँ") is not a confirm.
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
    # "haan" / "हाँ" at a confirm / identity slot scores 3 (explicit confirm),
    # not 0 (backchannel) — the borrower IS answering, not just nodding.
    # _explicit_confirm only fires at confirm/identity slots, so a "haan" at
    # a collect slot (plo_payment_intent) falls through to backchannel / cue.
    if _explicit_confirm(transcript, profile, awaited_slot):
        return {
            "evidence": 3,
            "evidence_reason": "explicit_confirm",
            "evidence_signals": {"slot": awaited_slot},
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

    cue = _cue_agree(transcript, profile)
    repeated = _borrower_repeated(transcript, state)
    if cue or repeated:
        return {
            "evidence": 2,
            "evidence_reason": "cue_agree" if cue else "borrower_repeated",
            "evidence_signals": {"cue": cue, "repeated": repeated},
        }

    if llm_calls >= 1:
        return {"evidence": 1, "evidence_reason": "llm_only", "evidence_signals": {}}

    return {"evidence": 0, "evidence_reason": "non_addressed", "evidence_signals": {}}
