"""UI-6B-1 — read-only health flags for the flow canvas."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.engine.nlg import COLLECT_SLOT_REPLY_IDS
from app.schemas.flow import FlowSet

VerdictFn = Callable[[str], dict[str, Any]]

SYSTEM_RAIL: list[dict[str, str]] = [
    {
        "id": "rail:policy_vulnerability",
        "label": "vulnerability",
        "tooltip": (
            "Policy interrupt: vulnerability language ends the collect path. "
            "Fonada safety layer — not editable."
        ),
    },
    {
        "id": "rail:policy_dnc",
        "label": "DNC",
        "tooltip": (
            "Policy interrupt: do-not-call / opt-out. Fonada safety layer — not editable."
        ),
    },
    {
        "id": "rail:policy_call_window",
        "label": "call window",
        "tooltip": (
            "Policy interrupt: outside the legal calling window. "
            "Fonada safety layer — not editable."
        ),
    },
    {
        "id": "rail:policy_third_party",
        "label": "third-party",
        "tooltip": (
            "Policy interrupt: third-party / DPDP disclosure lock. "
            "Fonada safety layer — not editable."
        ),
    },
    {
        "id": "rail:echo_filter",
        "label": "echo filter",
        "tooltip": "Drops agent-echo ASR before routing. Fonada safety layer — not editable.",
    },
    {
        "id": "rail:evidence_scorer",
        "label": "evidence",
        "tooltip": (
            "Scores whether the turn addressed the awaited slot. "
            "Fonada safety layer — not editable."
        ),
    },
    {
        "id": "rail:commitment_gate",
        "label": "commitment gate",
        "tooltip": (
            "Blocks money-state writes without evidence. Fonada safety layer — not editable."
        ),
    },
    {
        "id": "rail:compliance_gate",
        "label": "compliance",
        "tooltip": (
            "Pressure / prohibited-phrase gate on outbound text. "
            "Fonada safety layer — not editable."
        ),
    },
    {
        "id": "rail:resume",
        "label": "resume",
        "tooltip": (
            "Canonical resume onto the parked collect after an objection. "
            "Fonada safety layer — not editable."
        ),
    },
]


def attach_system_rail(graph: dict[str, Any]) -> dict[str, Any]:
    nodes = list(graph.get("nodes") or [])
    existing = {n["id"] for n in nodes}
    for i, rail in enumerate(SYSTEM_RAIL):
        if rail["id"] in existing:
            continue
        nodes.append(
            {
                "id": rail["id"],
                "kind": "system_rail",
                "text": rail["label"],
                "reply_id": None,
                "slot": None,
                "action": None,
                "index": -100 + i,
                "locked": True,
                "tooltip": rail["tooltip"],
            }
        )
    graph["nodes"] = nodes
    return graph


def annotate_graph_health(
    graph: dict[str, Any],
    flow_set: FlowSet,
    *,
    catalog_ids: set[str],
    verdict_fn: VerdictFn | None = None,
) -> dict[str, Any]:
    nodes = list(graph.get("nodes") or [])
    edges = list(graph.get("edges") or [])
    node_ids = {n["id"] for n in nodes}
    incoming: dict[str, int] = {n["id"]: 0 for n in nodes}
    for e in edges:
        if e.get("to") in incoming:
            incoming[e["to"]] += 1
        elif e.get("to") not in node_ids:
            incoming[str(e.get("to"))] = incoming.get(str(e.get("to")), 0)

    start_id = None
    ranked = [n for n in nodes if int(n.get("index") or 0) >= 0]
    if ranked:
        start_id = min(ranked, key=lambda n: int(n.get("index") or 0))["id"]

    issues: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}

    for node in nodes:
        nid = str(node["id"])
        kind = str(node.get("kind") or "")
        reasons: list[dict[str, str]] = []
        if kind == "flow_ref" or node.get("target_flow"):
            node["kind"] = "flow_ref"
            node["locked"] = False
        if kind == "system_rail":
            node["locked"] = True
            node["health"] = {"level": None, "reasons": []}
            by_id[nid] = node
            continue
        if kind == "action":
            node["locked"] = True
        else:
            node.setdefault("locked", False)

        if kind == "collect":
            slot = str(node.get("slot") or "")
            reask_id = COLLECT_SLOT_REPLY_IDS.get(slot)
            variants = flow_set.responses.get(reask_id or "") or []
            node["reask_count"] = len(variants) if reask_id else 0
            node["reask_id"] = reask_id
            if not reask_id:
                reasons.append(
                    {"level": "warning", "code": "collect_no_reask", "detail": slot or nid}
                )
            has_esc = any(
                e.get("from") == nid and e.get("kind") == "escalate_to" for e in edges
            )
            if not has_esc:
                reasons.append(
                    {
                        "level": "error",
                        "code": "collect_no_escalate",
                        "detail": slot or nid,
                    }
                )

        if kind == "utter" and verdict_fn:
            reply_id = str(node.get("reply_id") or "")
            variants = flow_set.responses.get(reply_id) or []
            node["reask_count"] = len(variants)
            for var in variants:
                text = (var.text or "").strip()
                if not text:
                    continue
                verdict = verdict_fn(text)
                if verdict.get("verdict") == "fail":
                    reasons.append(
                        {
                            "level": "error",
                            "code": "compliance_fail",
                            "detail": f"{reply_id}: {verdict.get('reason')}",
                        }
                    )
                    break

        if kind not in {"system_rail"} and nid != start_id and nid != "end":
            if incoming.get(nid, 0) == 0 and not str(nid).startswith("flow:"):
                reasons.append({"level": "info", "code": "orphan", "detail": nid})

        level = None
        if any(r["level"] == "error" for r in reasons):
            level = "error"
        elif any(r["level"] == "warning" for r in reasons):
            level = "warning"
        elif any(r["level"] == "info" for r in reasons):
            level = "info"
        node["health"] = {"level": level, "reasons": reasons}
        for r in reasons:
            issues.append({"node_id": nid, **r})
        by_id[nid] = node

    for e in edges:
        tgt = str(e.get("to") or "")
        if tgt in node_ids or tgt == "end":
            continue
        if tgt in catalog_ids or tgt.startswith("flow:"):
            hop = tgt.split(":", 1)[-1]
            if hop in catalog_ids or tgt in catalog_ids:
                continue
        issues.append(
            {
                "node_id": e.get("from"),
                "level": "error",
                "code": "dangling_target",
                "detail": tgt,
            }
        )
        src = by_id.get(str(e.get("from")))
        if src is not None:
            src.setdefault("health", {}).setdefault("reasons", []).append(
                {"level": "error", "code": "dangling_target", "detail": tgt}
            )
            src["health"]["level"] = "error"

    errors = sum(1 for i in issues if i["level"] == "error")
    warnings = sum(1 for i in issues if i["level"] == "warning")
    infos = sum(1 for i in issues if i["level"] == "info")
    graph["nodes"] = list(by_id.values()) if by_id else nodes
    graph["health"] = {
        "errors": errors,
        "warnings": warnings,
        "orphans": infos,
        "issues": issues,
    }
    return graph


def scan_tenant_health(
    graphs: list[dict[str, Any]],
) -> dict[str, Any]:
    errors = 0
    warnings = 0
    orphans = 0
    per_flow: list[dict[str, Any]] = []
    for g in graphs:
        h = g.get("health") or {}
        errors += int(h.get("errors") or 0)
        warnings += int(h.get("warnings") or 0)
        orphans += int(h.get("orphans") or 0)
        per_flow.append(
            {
                "flow_id": g.get("flow_id"),
                "errors": int(h.get("errors") or 0),
                "warnings": int(h.get("warnings") or 0),
                "orphans": int(h.get("orphans") or 0),
                "issues": h.get("issues") or [],
            }
        )
    return {
        "errors": errors,
        "warnings": warnings,
        "orphans": orphans,
        "flows": per_flow,
    }
