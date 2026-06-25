import re
from typing import Any

_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s\-]{8,14}\d)(?!\d)")
_ACCOUNT_RE = re.compile(r"\b\d{10,18}\b")


def mask_pii(text: str) -> str:
    """Mask phone-like and long numeric sequences in log strings."""
    masked = _PHONE_RE.sub("[PHONE]", text)
    return _ACCOUNT_RE.sub("[ACCOUNT]", masked)


def mask_pii_in_value(value: Any) -> Any:
    if isinstance(value, str):
        return mask_pii(value)
    if isinstance(value, dict):
        return {k: mask_pii_in_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_pii_in_value(item) for item in value]
    return value
