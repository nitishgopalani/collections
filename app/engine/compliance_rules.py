"""Shared compliance matching helpers."""

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.config import TenantConfig
from app.schemas.state import ConversationState


# Devanagari nukta (़ U+093C) — stripped so सख़्त/सख्त and डिफ़ॉल्ट/डिफॉल्ट match.
_DEVANAGARI_NUKTA = "\u093c"


def normalize(text: str) -> str:
    """Lowercase, collapse whitespace, strip Devanagari nukta (nukta-insensitive)."""
    collapsed = re.sub(r"\s+", " ", text.lower().strip())
    return collapsed.replace(_DEVANAGARI_NUKTA, "")


def flags(state: ConversationState) -> dict[str, Any]:
    raw = state.slots.get("compliance_flags")
    return dict(raw) if isinstance(raw, dict) else {}


def matches_any(text: str, phrases: list[str]) -> str | None:
    normalized = normalize(text)
    for phrase in phrases:
        token = normalize(phrase)
        if token and token in normalized:
            return phrase
    return None


def _word_bounded(text: str, start: int, end: int) -> bool:
    """True when ``text[start:end]`` is not glued to an alphanumeric neighbor."""
    if start > 0 and text[start - 1].isalnum():
        return False
    if end < len(text) and text[end].isalnum():
        return False
    return True


def find_substring_spans(
    normalized: str,
    phrase: str,
    *,
    word_bounded: bool = False,
) -> list[tuple[int, int]]:
    """All occurrences of ``phrase`` in ``normalized`` (exact substring, no regex)."""
    token = normalize(phrase)
    if not token:
        return []
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        idx = normalized.find(token, start)
        if idx < 0:
            break
        end = idx + len(token)
        if not word_bounded or _word_bounded(normalized, idx, end):
            spans.append((idx, end))
        start = idx + 1
    return spans


def evaluate_pressure_with_allowlist(
    text: str,
    pressure_phrases: list[str],
    allowlisted_phrases: list[str],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Return ``(blocking_phrase, warnings)`` for collection-pressure matches.

    A pressure match is exempt (warning only) when some allowlisted phrase appears
    as a word-bounded exact substring of the normalized reply and fully covers
    the pressure match span. Partial overlap / missing allowlist text does not
    exempt. Non-covered matches block exactly as today.
    """
    normalized = normalize(text)
    allowlist = [p for p in allowlisted_phrases if (p or "").strip()]
    warnings: list[dict[str, Any]] = []

    for phrase in pressure_phrases:
        # Same match semantics as ``matches_any`` / ``is_collection_pressure``.
        pressure_spans = find_substring_spans(normalized, phrase, word_bounded=False)
        if not pressure_spans:
            continue
        for p_start, p_end in pressure_spans:
            covering: str | None = None
            for allow in allowlist:
                for a_start, a_end in find_substring_spans(
                    normalized, allow, word_bounded=True
                ):
                    if a_start <= p_start and a_end >= p_end:
                        covering = allow
                        break
                if covering is not None:
                    break
            if covering is not None:
                warnings.append(
                    {
                        "kind": "collection_pressure",
                        "phrase": phrase,
                        "allowlisted_phrase": covering,
                        "allowlisted": True,
                    }
                )
            else:
                return phrase, warnings
    return None, warnings


def is_collection_pressure(text: str, tenant_cfg: TenantConfig) -> bool:
    return matches_any(text, tenant_cfg.collection_pressure_phrases) is not None


def parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":", 1)
    return int(hour), int(minute)


def within_call_window(tenant_cfg: TenantConfig, now: datetime) -> bool:
    tz = ZoneInfo(tenant_cfg.call_window_timezone)
    local = now.astimezone(tz)
    start_h, start_m = parse_hhmm(tenant_cfg.call_window_start)
    end_h, end_m = parse_hhmm(tenant_cfg.call_window_end)
    current = local.hour * 60 + local.minute
    start = start_h * 60 + start_m
    end = end_h * 60 + end_m
    return start <= current <= end
