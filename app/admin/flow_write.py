"""UI-6B-3 — apply a builder graph onto flow YAML (after the publish gate)."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.admin.replies import _flow_files
from app.admin.yaml_io import dump_raw, load_raw
from app.flows.loader import FLOWS_DIR, reload_flow_set

_FLOW_HEAD = re.compile(r"(?m)^  ([a-zA-Z0-9_]+):\s*$")
_BUILDER_IF = '{slot} == "__flow_builder_obj__"'
_FB_HOP = "_fb_hop_"


def find_flow_yaml(flow_id: str) -> Path | None:
    fid = (flow_id or "").strip()
    if not fid:
        return None
    for path in _flow_files():
        text = path.read_text(encoding="utf-8")
        for match in _FLOW_HEAD.finditer(text):
            if match.group(1) == fid:
                return path
    return None


def rel_flow_path(path: Path) -> str:
    try:
        return path.relative_to(FLOWS_DIR.parent.parent).as_posix()
    except ValueError:
        return path.as_posix()


def unwrap_target(target: str) -> str:
    raw = (target or "").strip()
    if raw.startswith("flow:"):
        return raw.split(":", 1)[-1]
    return raw


def _hop_step_id(flow_name: str) -> str:
    return _FB_HOP + unwrap_target(flow_name).replace(":", "_")


def _ensure_hop_step(
    steps: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    flow_name: str,
) -> str:
    sid = _hop_step_id(flow_name)
    if sid in by_id:
        return sid
    step = {"id": sid, "next": "end"}
    steps.append(step)
    by_id[sid] = step
    return sid


def _slot_for_decide(step: dict[str, Any]) -> str:
    decide = step.get("decide") or []
    for br in decide:
        if not isinstance(br, dict):
            continue
        if_ = str(br.get("if") or "")
        if "==" in if_:
            return if_.split("==", 1)[0].strip().strip('"')
    return "plo_scenario"


def _next_decide_id(by_id: dict[str, dict[str, Any]], start: str) -> str | None:
    """Walk ``next`` until a step that already has ``decide`` (never invent one)."""
    seen: set[str] = set()
    cur = start
    while cur and cur not in seen:
        seen.add(cur)
        step = by_id.get(cur)
        if step is None:
            return None
        if step.get("decide") is not None:
            return cur
        nxt = step.get("next")
        if not isinstance(nxt, str) or not nxt.strip():
            return None
        cur = unwrap_target(nxt)
    return None


def _rehost_start_flow(
    by_id: dict[str, dict[str, Any]],
    outgoing: dict[str, list[dict[str, Any]]],
) -> None:
    """Objection hops from collect/utter attach to the next decide, not a new decide."""
    for sid, elist in list(outgoing.items()):
        hops = [e for e in elist if e.get("kind") == "start_flow"]
        if not hops:
            continue
        step = by_id.get(sid)
        if step is not None and step.get("decide") is not None:
            continue
        host = _next_decide_id(by_id, sid)
        if host is None or host == sid:
            continue
        outgoing[sid] = [e for e in elist if e.get("kind") != "start_flow"]
        outgoing[host].extend(hops)


def apply_graph_to_yaml(
    flow_id: str,
    *,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> list[Path]:
    path = find_flow_yaml(flow_id)
    if path is None:
        raise FileNotFoundError(flow_id)
    raw = load_raw(path)
    flows = raw.get("flows") or {}
    flow = flows.get(flow_id)
    if not isinstance(flow, dict):
        raise KeyError(flow_id)
    steps = list(flow.get("steps") or [])
    by_id: dict[str, dict[str, Any]] = {}
    for step in steps:
        if isinstance(step, dict) and step.get("id"):
            by_id[str(step["id"])] = step

    outgoing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in edges:
        src = str(edge.get("from") or "")
        if src.startswith("rail:") or src.startswith("flow:"):
            continue
        outgoing[src].append(edge)

    _rehost_start_flow(by_id, outgoing)

    for sid, elist in outgoing.items():
        step = by_id.get(sid)
        if step is None:
            continue
        esc = [e for e in elist if e.get("kind") == "escalate_to"]
        if esc:
            step["escalate_to"] = unwrap_target(str(esc[0].get("to") or ""))
        nxt = [e for e in elist if e.get("kind") == "next"]
        if nxt:
            dest = unwrap_target(str(nxt[0].get("to") or ""))
            if dest and dest != sid:
                step["next"] = dest
        dec = [e for e in elist if e.get("kind") in {"decide", "start_flow"}]
        if dec and step.get("decide") is not None:
            ifs: list[dict[str, Any]] = []
            elses: list[dict[str, Any]] = []
            seen: set[tuple[str, str]] = set()
            for e in dec:
                dest = unwrap_target(str(e.get("to") or ""))
                if not dest:
                    continue
                label = str(e.get("label") or "").strip()
                kind = str(e.get("kind") or "")
                if kind == "start_flow" and dest not in by_id:
                    dest = _ensure_hop_step(steps, by_id, dest)
                    slot = _slot_for_decide(step)
                    label = _BUILDER_IF.format(slot=slot)
                if label in {"else", ""}:
                    key = ("else", dest)
                    if key in seen:
                        continue
                    seen.add(key)
                    elses.append({"else": dest})
                    continue
                key = (label, dest)
                if key in seen:
                    continue
                seen.add(key)
                ifs.append({"if": label, "then": dest})
            rebuilt = ifs + elses
            if rebuilt:
                step["decide"] = rebuilt

    responses = raw.setdefault("responses", {})
    for node in nodes:
        if str(node.get("kind") or "") != "utter":
            continue
        reply_id = str(node.get("reply_id") or "").strip()
        if not reply_id:
            continue
        text = str(node.get("full_text") or node.get("text") or "").strip()
        if not text:
            continue
        variants = responses.get(reply_id)
        if isinstance(variants, list) and variants and isinstance(variants[0], dict):
            variants[0]["text"] = text

    referenced: set[str] = set()
    for step in steps:
        if not isinstance(step, dict):
            continue
        for br in step.get("decide") or []:
            if isinstance(br, dict):
                if br.get("then"):
                    referenced.add(str(br["then"]))
                if br.get("else"):
                    referenced.add(str(br["else"]))
        nxt = step.get("next")
        if isinstance(nxt, str):
            referenced.add(nxt)
    steps[:] = [
        s
        for s in steps
        if not (
            isinstance(s, dict)
            and str(s.get("id") or "").startswith(_FB_HOP)
            and str(s.get("id")) not in referenced
        )
    ]
    flow["steps"] = steps

    dump_raw(path, raw)
    reload_flow_set()
    return [path]
