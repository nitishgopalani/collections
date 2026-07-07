"""Voicemail / carrier-machine detection for property consult legs.

Two-tier transcript heuristic (AMD stays off for now):

* **Strong** phrases fire alone — they only occur on actual voicemail / mailbox
  prompts ("record your message", "रिकॉर्ड", "mailbox", "beep").
* **Weak** phrases are common carrier messages that are NOT voicemail on their
  own ("not available", "busy", "unavailable") — they fire only when paired
  with another weak/strong phrase within the same 3-turn window.

"forwarded" / "forward" are explicitly excluded — they are the carrier's
normal call-connection announcement, not voicemail. (Live false-positive
2026-07-07: transcript "हो है बीन फॉरवर्ड।" fired VM on a real answer.)
"""

from __future__ import annotations

import re
from functools import lru_cache

from app.config import get_settings

# Strong phrases — fire alone. Only real VM/mailbox prompts.
_DEFAULT_STRONG_PHRASES = (
    "record your message",
    "record a message",
    "please record",
    "finished recording",
    "after the tone",
    "after the beep",
    "leave a message",
    "leave your message",
    "voice mail",
    "voicemail",
    "mailbox",
    "रिकॉर्ड योर मैसेज",
    "संदेश रिकॉर्ड",
    "रिकॉर्ड कर",
    "रिकॉर्ड कीजिए",
    "बीप के बाद",
    "टोन के बाद",
    "संदेश छोड़",
)

# Weak phrases — fire only in conjunction with another weak/strong hit.
_DEFAULT_WEAK_PHRASES = (
    "not available",
    "not reachable",
    "unavailable",
    "cannot be completed",
    "is busy",
    "currently busy",
    "उपलब्ध नहीं",
    "उपलब्ध नही",
    "व्यस्त है",
    "फोन नहीं उठा",
)


@lru_cache(maxsize=1)
def _phrase_lists() -> tuple[tuple[re.Pattern[str], ...], tuple[re.Pattern[str], ...]]:
    settings = get_settings()
    raw = (getattr(settings, "consult_voicemail_phrases", "") or "").strip()
    if raw:
        strong_raw, sep, weak_raw = raw.partition("|")
        strong = [p.strip() for p in strong_raw.split(",") if p.strip()]
        weak = [p.strip() for p in weak_raw.split(",") if p.strip()] if sep else []
    else:
        strong = list(_DEFAULT_STRONG_PHRASES)
        weak = list(_DEFAULT_WEAK_PHRASES)
    strong_pats = tuple(re.compile(re.escape(p.lower()), re.IGNORECASE) for p in strong)
    weak_pats = tuple(re.compile(re.escape(p.lower()), re.IGNORECASE) for p in weak)
    return strong_pats, weak_pats


def _matches(text: str, patterns: tuple[re.Pattern[str], ...]) -> list[str]:
    hits: list[str] = []
    for pat in patterns:
        if pat.search(text):
            hits.append(pat.pattern)
    return hits


def classify_transcript(transcript: str) -> tuple[list[str], list[str]]:
    """Return (strong_hits, weak_hits) for a single transcript.

    Strong phrases fire alone. Weak phrases require conjunction (another
    weak or strong hit) — see ``is_voicemail_transcript`` for the per-turn
    rule, and ``_try_voicemail_abort`` in prompt_agent for the 3-turn
    window accumulation of weak hits.
    """
    text = (transcript or "").strip().lower()
    if not text:
        return [], []
    strong_pats, weak_pats = _phrase_lists()
    return _matches(text, strong_pats), _matches(text, weak_pats)


def is_voicemail_transcript(transcript: str) -> bool:
    """True when ASR text looks like a carrier VM / mailbox prompt.

    Single-transcript rule: a strong phrase fires alone; a weak phrase fires
    only if another weak/strong phrase also matches in the SAME transcript.
    For across-turn weak+weak conjunction within the 3-turn window, see
    ``_try_voicemail_abort`` in prompt_agent (it accumulates weak hits).
    """
    strong_hits, weak_hits = classify_transcript(transcript)
    if strong_hits:
        return True
    return len(weak_hits) >= 2


def reset_phrase_cache() -> None:
    """Test helper after settings/env mutation."""
    _phrase_lists.cache_clear()
