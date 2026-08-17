"""UI-6B-1 — layout sidecars. Never written into flow YAML."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.flows.loader import FLOWS_DIR

_SAFE_ID = re.compile(r"^[a-zA-Z0-9._-]+$")
LAYOUTS_DIR = FLOWS_DIR / "_layouts"


def layout_path(flow_id: str) -> Path:
    fid = (flow_id or "").strip()
    if not _SAFE_ID.match(fid):
        raise ValueError("invalid flow_id")
    return LAYOUTS_DIR / f"{fid}.layout.json"


def read_layout(flow_id: str) -> dict[str, Any]:
    path = layout_path(flow_id)
    if not path.is_file():
        return {"flow_id": flow_id, "nodes": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"flow_id": flow_id, "nodes": []}
    if not isinstance(data, dict):
        return {"flow_id": flow_id, "nodes": []}
    data.setdefault("flow_id", flow_id)
    data.setdefault("nodes", [])
    return data


def write_layout(flow_id: str, nodes: list[dict[str, Any]]) -> dict[str, Any]:
    LAYOUTS_DIR.mkdir(parents=True, exist_ok=True)
    clean: list[dict[str, Any]] = []
    for row in nodes:
        nid = str(row.get("id") or "").strip()
        if not nid:
            continue
        try:
            x = float(row.get("x"))
            y = float(row.get("y"))
        except (TypeError, ValueError):
            continue
        clean.append({"id": nid, "x": round(x, 2), "y": round(y, 2)})
    payload = {"flow_id": flow_id, "nodes": clean}
    path = layout_path(flow_id)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload
