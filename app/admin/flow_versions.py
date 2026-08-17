"""UI-6B-3 — flow YAML version snapshots (not in git)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.flows.loader import FLOWS_DIR, reload_flow_set

_SAFE = re.compile(r"^[a-zA-Z0-9._-]+$")
VERSIONS_DIR = FLOWS_DIR / "_versions"


def _tenant_dir(tenant_id: str) -> Path:
    tid = (tenant_id or "").strip()
    if not _SAFE.match(tid):
        raise ValueError("invalid tenant_id")
    return VERSIONS_DIR / tid


def list_versions(tenant_id: str) -> list[dict[str, Any]]:
    folder = _tenant_dir(tenant_id)
    if not folder.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(folder.glob("v*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        rows.append(
            {
                "version": int(data.get("version") or 0),
                "ts": data.get("ts"),
                "flow_id": data.get("flow_id"),
                "note": data.get("note") or "",
                "files": list((data.get("files") or {}).keys()),
            }
        )
    rows.sort(key=lambda r: r["version"])
    return rows


def next_version(tenant_id: str) -> int:
    existing = [r["version"] for r in list_versions(tenant_id) if r["version"]]
    return (max(existing) + 1) if existing else 1


def snapshot(
    tenant_id: str,
    *,
    flow_id: str,
    files: dict[str, str],
    note: str = "",
    version: int | None = None,
) -> dict[str, Any]:
    folder = _tenant_dir(tenant_id)
    folder.mkdir(parents=True, exist_ok=True)
    ver = int(version or next_version(tenant_id))
    payload = {
        "version": ver,
        "ts": datetime.now(UTC).isoformat(),
        "tenant_id": tenant_id,
        "flow_id": flow_id,
        "note": note,
        "files": files,
    }
    path = folder / f"v{ver}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"version": ver, "ts": payload["ts"], "flow_id": flow_id, "note": note}


def load_snapshot(tenant_id: str, version: int) -> dict[str, Any]:
    path = _tenant_dir(tenant_id) / f"v{int(version)}.json"
    if not path.is_file():
        raise FileNotFoundError(str(version))
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("invalid snapshot")
    return data


def restore_files(files: dict[str, str]) -> list[Path]:
    written: list[Path] = []
    for rel, content in files.items():
        path = Path(rel)
        if not path.is_absolute():
            path = FLOWS_DIR.parent.parent / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)
    reload_flow_set()
    return written


def revert(tenant_id: str, version: int) -> dict[str, Any]:
    data = load_snapshot(tenant_id, version)
    files = data.get("files") or {}
    if not isinstance(files, dict):
        raise ValueError("invalid snapshot files")
    restore_files({str(k): str(v) for k, v in files.items()})
    return {
        "ok": True,
        "version": int(data.get("version") or version),
        "flow_id": data.get("flow_id"),
        "restored": list(files.keys()),
    }
