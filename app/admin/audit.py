"""Append-only admin write audit (ts, endpoint, before/after hash)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import get_settings


def file_hash(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def payload_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def audit_write(endpoint: str, *, before: str, after: str, extra: dict[str, Any] | None = None) -> None:
    settings = get_settings()
    path = Path(settings.admin_audit_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now(UTC).isoformat(),
        "endpoint": endpoint,
        "before": before,
        "after": after,
    }
    if extra:
        row.update(extra)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
