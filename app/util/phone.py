"""Phone normalization for borrower lookup (India E.164 variants)."""

from __future__ import annotations

import re

_NON_DIGIT_RE = re.compile(r"\D")
_DEFAULT_COUNTRY = "91"


def digits_only(phone: str) -> str:
    return _NON_DIGIT_RE.sub("", str(phone).strip())


def canonical_phone(phone: str, *, country_code: str = _DEFAULT_COUNTRY) -> str:
    """Normalize to E.164 where possible (e.g. 9810587857 → +919810587857)."""
    raw = str(phone).strip()
    d = digits_only(raw)
    if not d:
        return ""
    if len(d) == 10 and country_code:
        return f"+{country_code}{d}"
    if len(d) == 11 and d.startswith("0") and country_code:
        return f"+{country_code}{d[1:]}"
    if country_code and d.startswith(country_code) and len(d) >= len(country_code) + 10:
        return f"+{d}"
    if raw.startswith("+"):
        return f"+{d}"
    return d


def phone_match_suffix(phone: str) -> str:
    """Last 10 digits — matches +91…, 91…, and bare 10-digit Indian mobile."""
    d = digits_only(phone)
    if len(d) >= 10:
        return d[-10:]
    return d


def phone_lookup_variants(phone: str, *, country_code: str = _DEFAULT_COUNTRY) -> frozenset[str]:
    """All common forms for exact-match fallback."""
    d = digits_only(phone)
    if not d:
        return frozenset()
    variants: set[str] = {d, raw.strip() if (raw := str(phone).strip()) else d}
    suffix = phone_match_suffix(phone)
    if suffix:
        variants.add(suffix)
        variants.add(f"{country_code}{suffix}")
        variants.add(f"+{country_code}{suffix}")
    canonical = canonical_phone(phone, country_code=country_code)
    if canonical:
        variants.add(canonical)
        variants.add(digits_only(canonical))
    return frozenset(v for v in variants if v)
