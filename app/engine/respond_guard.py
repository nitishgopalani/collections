"""Tier-3 respond fact-grounding — hard swap on any ungrounded numeric token."""

from __future__ import annotations

import re
from typing import Any

# ₹-amounts, grouped/plain digits, ISO and slash dates.
_NUMERIC_TOKEN_RE = re.compile(
    r"(?:₹\s*[\d,]+(?:\.\d+)?)"
    r"|(?:\d{4}-\d{2}-\d{2})"
    r"|(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4})"
    r"|(?<!\w)\d{1,3}(?:,\d{2,3})+(?:\.\d+)?(?!\w)"
    r"|(?<!\w)\d+(?:\.\d+)?(?!\w)",
    re.UNICODE,
)


def normalize_numeric_fragment(value: str) -> str:
    """Strip ₹, commas, and whitespace for containment checks."""
    return re.sub(r"[\s,₹]", "", value or "")


def extract_numeric_tokens(text: str) -> list[str]:
    return _NUMERIC_TOKEN_RE.findall(text or "")


def _slot_values_blob(slots: dict[str, Any]) -> str:
    parts: list[str] = []
    for value in slots.values():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            continue
        parts.append(normalize_numeric_fragment(str(value)))
    return " ".join(parts)


def ground_respond_text(
    respond_text: str,
    slots: dict[str, Any],
    unknown_info_reply: str,
) -> tuple[str, str]:
    """Return ``(text, grounding_result)`` where result is ``pass`` or ``swapped``.

    Every numeric token (₹, digits, date-like strings) in ``respond_text`` must
    appear in hydrated slot VALUES (normalized string containment). Any miss
    replaces the ENTIRE text with ``unknown_info_reply`` — never a partial edit.
    """
    text = (respond_text or "").strip()
    if not text:
        return (unknown_info_reply or "").strip(), "swapped"

    blob = _slot_values_blob(slots)
    for token in extract_numeric_tokens(text):
        needle = normalize_numeric_fragment(token)
        if needle and needle not in blob:
            return (unknown_info_reply or "").strip(), "swapped"
    return text, "pass"
