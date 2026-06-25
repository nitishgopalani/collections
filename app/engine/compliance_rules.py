"""Shared compliance matching helpers."""

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.config import TenantConfig
from app.schemas.state import ConversationState


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def flags(state: ConversationState) -> dict[str, Any]:
    raw = state.slots.get("compliance_flags")
    return dict(raw) if isinstance(raw, dict) else {}


def matches_any(text: str, phrases: list[str]) -> str | None:
    normalized = normalize(text)
    for phrase in phrases:
        token = phrase.lower().strip()
        if token and token in normalized:
            return phrase
    return None


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


def is_collection_pressure(text: str, tenant_cfg: TenantConfig) -> bool:
    return matches_any(text, tenant_cfg.collection_pressure_phrases) is not None
