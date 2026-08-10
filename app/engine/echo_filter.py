"""W2-1 Echo filter — detect when ASR feeds back the bot's own last spoken reply.

Runs BEFORE policy preempts (safety / dnc / call_window / third_party) so the
bot's own spoken legal lines (DNC ack, vulnerability close, third-party script,
opener greeting) cannot self-trigger the policy lane when the speaker echo
leaks back into the mic. On echo match the turn is dropped: outcome=HOLD,
``echo_suspected=true``, ``evidence=0``, zero counter burn (no attempts++, no
LLM call, no flow advance).

Devanagari-aware: reuses ``_tokenize`` (scripted_coercions) for token overlap
and ``normalize`` (compliance_rules) for nukta-insensitive substring match.
Threshold is env-configurable (``ECHO_MATCH_THRESHOLD``, default 0.7) — high
enough that a genuine borrower response with partial overlap is not
suppressed, low enough to catch a near-exact speaker echo.
"""

from __future__ import annotations

import os

from app.engine.compliance_rules import normalize
from app.engine.scripted_coercions import _tokenize

_DEFAULT_THRESHOLD = 0.7
# Don't flag tiny transcripts (1-2 tokens) on Jaccard alone — require a near-
# exact substring match so a bare "haan" or "theek" is never misread as echo.
_MIN_TOKENS_FOR_JACCARD = 3


def echo_match_threshold() -> float:
    """Env-configurable Jaccard threshold (default 0.7)."""
    v = os.getenv("ECHO_MATCH_THRESHOLD")
    if v:
        try:
            return float(v)
        except ValueError:
            pass
    return _DEFAULT_THRESHOLD


def detect_echo(
    transcript: str,
    last_spoken_reply: str,
    *,
    threshold: float | None = None,
) -> bool:
    """True when ``transcript`` is a near-repeat of the bot's last spoken reply.

    Two paths:
    1. Exact normalized match (``normalize(t) == normalize(r)``) — catches the
       clean speaker-echo case where ASR heard the bot's line verbatim.
    2. High Jaccard token overlap (>= threshold) for transcripts with at least
       ``_MIN_TOKENS_FOR_JACCARD`` tokens — catches partial / noisy echo. Short
       transcripts only flag on a near-exact substring of the reply.
    """
    t = (transcript or "").strip()
    r = (last_spoken_reply or "").strip()
    if not t or not r:
        return False

    if normalize(t) == normalize(r):
        return True

    t_tokens = _tokenize(t.lower())
    r_tokens = _tokenize(r.lower())
    if not t_tokens or not r_tokens:
        return False

    # Short transcripts (1-2 tokens) are never echo on overlap alone — a bare
    # "haan" / "theek" / "ramesh" is a real answer, not a speaker echo. Only
    # an exact normalized match (handled above) would flag them.
    if len(t_tokens) < _MIN_TOKENS_FOR_JACCARD:
        return False

    # Fragment echo: the transcript is a contiguous substring of the bot's
    # last spoken reply (ASR heard a chunk of the bot's line). Contiguous
    # substring of 3+ tokens is specific enough to not catch a real answer.
    if normalize(t) in normalize(r):
        return True

    # Overlap echo: high Jaccard token overlap (near-complete repeat with
    # some words dropped/noisy).
    inter = t_tokens & r_tokens
    union = t_tokens | r_tokens
    jaccard = len(inter) / len(union) if union else 0.0
    thr = threshold if threshold is not None else echo_match_threshold()
    return jaccard >= thr
