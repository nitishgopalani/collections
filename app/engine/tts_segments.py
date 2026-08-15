"""DEBT-038: split spoken replies around hydrated slot values.

Static prefix/suffix become their own TTS cache keys (prewarmable). The
dynamic slot (usually customer_name) is a short live segment.
"""

from __future__ import annotations

import re
from typing import Any

_SLOT_SPLIT_KEYS = (
    "customer_name",
    "borrower_name",
    "branch",
    "helpline",
)


def segment_spoken_reply(text: str, slots: dict[str, Any] | None = None) -> list[str]:
    """Return 1–3 segments: static prefix, slot value, static suffix."""
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    slots = slots or {}
    for key in _SLOT_SPLIT_KEYS:
        value = str(slots.get(key) or "").strip()
        if len(value) < 2:
            continue
        idx = cleaned.find(value)
        if idx < 0:
            continue
        prefix = cleaned[:idx]
        suffix = cleaned[idx + len(value) :]
        parts = [p for p in (prefix, value, suffix) if p]
        if len(parts) >= 2:
            return parts
    return [cleaned]


_TEMPLATE_SLOT_RE = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")


def split_template_static(template: str) -> list[str]:
    """Static prefix/suffix around ``{slot}`` tokens — boot-prewarm keys."""
    if not template:
        return []
    parts = _TEMPLATE_SLOT_RE.split(template)
    return [p for p in parts if p.strip()]
