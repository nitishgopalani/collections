"""Declarative slot validation — repair layer F4 (tenant-agnostic).

The LLM occasionally emits a ``set_slot`` that would quietly corrupt the flow:

* overwriting a hydrated *fact* (``due_date``, amounts, ...) — this poisons
  downstream classifiers (e.g. ``classify_sot_commit_timing`` reads ``due_date``);
* filling a *typed* slot with the wrong kind of answer (a day/ISO date where a
  clock time is expected) — the slot then "advances" on garbage.

We drop those commands *before* they are applied. Dropping leaves the awaited slot
empty, so the executor re-asks it — which the retry-cap (F1) + rephrase-on-repeat
(F2) then bound and escalate gracefully. Validation is generic: register a slot in
``FACT_SLOTS`` or ``SLOT_TYPE_VALIDATORS`` and it is enforced for every tenant.

Enum slots are deliberately NOT hard-rejected here: the flows route unknown enum
values through ``else`` branches (and the SOT coercions normalise bare yes/no), so
rejecting them would add risk without benefit.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from app.schemas.command import Command

# Hydrated borrower/account facts the LLM must never rewrite mid-call. These are
# never flow ``collect`` targets, so dropping an LLM write is always safe and keeps
# classifiers/offers reading the real values.
FACT_SLOTS: frozenset[str] = frozenset(
    {
        "due_date",
        "repay_amount",
        "offer_amount",
        "discount_amount",
        "loan_amount",
        "disbursal_date",
        "amount_due",
        "dpd",
        "bucket",
        "account_ref",
        "borrower_name",
        "borrower_phone",
        "phone",
    }
)

_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")

# Clock-time cues (a real "kis time tak?" answer). Devanagari + Roman + digits.
_TIME_CUES: tuple[str, ...] = (
    "baje", "bje", "am", "pm", "a.m", "p.m", ":",
    "subah", "subeh", "dopahar", "dopeher", "dopahr",
    "shaam", "sham", "raat", "raatko", "noon", "midnight",
    "morning", "evening", "afternoon", "night", "o'clock", "oclock",
    "बजे", "सुबह", "दोपहर", "शाम", "रात",
)
# Day/relative-date words: a bare one of these is NOT a clock time.
_DAY_CUES: tuple[str, ...] = (
    "kal", "parso", "parson", "aaj", "today", "tomorrow", "yesterday",
    "din baad", "hafte", "week", "mahine", "month", "due date", "duedate",
    "somvar", "mangalvar", "budhvar", "guruvar", "shukravar", "shanivar", "ravivar",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "कल", "परसों", "आज", "दिन बाद", "हफ्ते", "महीने",
)


def _has_any(text: str, cues: tuple[str, ...]) -> bool:
    return any(cue in text for cue in cues)


def is_valid_clock_time(value: object) -> bool:
    """True if ``value`` reads as a time-of-day rather than a bare day / ISO date.

    Accepts anything containing a time cue ("shaam 6 baje", "6:30", "raat tak").
    Rejects an ISO date, or a bare day word with no time cue ("kal", "parso",
    "due date ko"). Ambiguous free text with neither cue is accepted (we prefer a
    false-accept over re-asking a borrower who gave a fuzzy-but-real time).
    """
    text = str(value or "").strip().lower()
    if not text:
        return False
    if _ISO_DATE_RE.search(text):
        return _has_any(text, _TIME_CUES)  # ISO alone is invalid; ISO + time survives
    if _has_any(text, _TIME_CUES):
        return True
    if _has_any(text, _DAY_CUES):
        return False
    return True


# slot name -> predicate(value) -> is_valid. Add tenant slots here to enforce a type.
SLOT_TYPE_VALIDATORS: dict[str, Callable[[object], bool]] = {
    "sot_customer_time": is_valid_clock_time,
}


def validate_commands(commands: list[Command]) -> tuple[list[Command], list[str]]:
    """Drop set_slot commands that would corrupt the flow. Returns (kept, dropped).

    ``dropped`` is a list of human-readable reasons for the turn-decision log.
    Non-set_slot commands always pass through untouched.
    """
    if not commands:
        return commands, []
    kept: list[Command] = []
    dropped: list[str] = []
    for cmd in commands:
        if cmd.command != "set_slot":
            kept.append(cmd)
            continue
        name = str(cmd.name or "")
        if name in FACT_SLOTS:
            dropped.append(f"blocked write to fact slot {name}={cmd.value!r}")
            continue
        validator = SLOT_TYPE_VALIDATORS.get(name)
        if validator is not None and not validator(cmd.value):
            dropped.append(f"invalid value for {name}={cmd.value!r}")
            continue
        kept.append(cmd)
    return kept, dropped
