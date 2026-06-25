"""Safe condition evaluation for flow branches — no eval()."""

import re
from typing import Any

_COMPARATORS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}

_IN_PATTERN = re.compile(
    r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s+in\s+(.+)\s*$",
    re.IGNORECASE,
)
_CMP_PATTERN = re.compile(
    r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(==|!=|<=|>=|<|>)\s*(.+)\s*$",
)


def _parse_literal(raw: str) -> Any:
    text = raw.strip()
    lower = text.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower == "null" or lower == "none":
        return None
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_parse_literal(part) for part in inner.split(",")]
    if text.startswith('"') and text.endswith('"'):
        return text[1:-1]
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1]
    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        return text


def _resolve_slot(slots: dict[str, Any], name: str) -> Any:
    return slots.get(name)


def evaluate_condition(expression: str, slots: dict[str, Any]) -> bool:
    """Evaluate a branch condition against conversation slots."""
    expr = expression.strip()
    in_match = _IN_PATTERN.match(expr)
    if in_match:
        left_name, right_raw = in_match.group(1), in_match.group(2)
        left = _resolve_slot(slots, left_name)
        right = _parse_literal(right_raw)
        if isinstance(right, list):
            return left in right
        return False

    cmp_match = _CMP_PATTERN.match(expr)
    if not cmp_match:
        raise ValueError(f"Unsupported condition expression: {expression}")

    left_name, op, right_raw = cmp_match.group(1), cmp_match.group(2), cmp_match.group(3)
    left = _resolve_slot(slots, left_name)
    right = _parse_literal(right_raw)

    if op in ("<", "<=", ">", ">="):
        if left is None or right is None:
            return False
        return bool(_COMPARATORS[op](left, right))

    return bool(_COMPARATORS[op](left, right))
