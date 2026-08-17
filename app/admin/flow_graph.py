"""UI-6A — tenant flow catalog + per-flow graph (view only)."""

from __future__ import annotations

from typing import Any

from app.engine.catalog import (
    _flow_belongs_to_tenant,
    compress_flow_description,
    normalize_scenario_key,
)
from app.engine.tenant_profile import TenantRuntimeProfile
from app.schemas.flow import Flow, FlowBranch, FlowSet, FlowStep
from app.schemas.state import ConversationState

_CHAIN_TO_FLOW: dict[str, str] = {
    "plo_chain_predue": "plo_predue",
    "plo_chain_ondue": "plo_ondue",
    "plo_chain_postdue1": "plo_postdue1",
    "plo_chain_postdue2": "plo_postdue2",
    "plo_chain_postdue3": "plo_postdue3",
    "plo_chain_npa": "plo_npa",
}

_SCENARIO_DEFAULT_FLOW: dict[str, str] = {
    "predue": "plo_predue",
    "ondue": "plo_ondue",
    "postdue1": "plo_postdue1",
    "postdue2": "plo_postdue2",
    "postdue3": "plo_postdue3",
    "npa": "plo_npa",
}


def step_id(step: FlowStep, index: int) -> str:
    return (step.id or "").strip() or f"_i{index}"


def step_kind(step: FlowStep) -> str:
    if step.decide:
        return "decide"
    if step.collect:
        return "collect"
    if step.utter:
        return "utter"
    return "action"


def _preview(step: FlowStep, flow_set: FlowSet) -> str:
    if step.utter:
        variants = flow_set.responses.get(step.utter) or []
        if variants and (variants[0].text or "").strip():
            return (variants[0].text or "").strip()[:120]
        return step.utter
    if step.collect:
        return f"collect {step.collect}"
    if step.action:
        return str(step.action)
    if step.decide:
        return "decide"
    return ""


def _branch_targets(branches: list[FlowBranch]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for br in branches:
        label = (br.if_ or "").strip() or ("else" if br.else_ else "")
        dest = (br.then or br.else_ or "").strip()
        if dest:
            out.append((dest, label or "then"))
    return out


def _scenario_allows(flow: Flow, scenario: str | None) -> bool:
    if not scenario:
        return True
    if (flow.catalog_scope or "").strip().lower() == "universal":
        return True
    tags = [normalize_scenario_key(s) or str(s).strip().lower() for s in flow.scenarios]
    if not tags:
        return True
    want = normalize_scenario_key(scenario) or scenario.strip().lower()
    raw = scenario.strip().lower()
    return want in tags or raw in {str(s).strip().lower() for s in flow.scenarios}


def tenant_catalog(
    profile: TenantRuntimeProfile,
    flow_set: FlowSet,
    *,
    scenario: str | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in sorted(flow_set.flows):
        if not _flow_belongs_to_tenant(name, profile):
            continue
        flow = flow_set.flows[name]
        if not _scenario_allows(flow, scenario):
            continue
        rows.append(
            {
                "id": name,
                "description": compress_flow_description(flow.description),
                "scenarios": list(flow.scenarios or []),
                "catalog_scope": flow.catalog_scope,
                "priority": flow.priority,
                "default_for": [
                    sid
                    for sid, fid in _SCENARIO_DEFAULT_FLOW.items()
                    if fid == name
                ],
            }
        )
    return rows


def default_flow_id(tenant_prefix: str, scenario: str | None) -> str:
    scen = (scenario or "").strip().lower()
    mapped = _SCENARIO_DEFAULT_FLOW.get(scen)
    if mapped and mapped.startswith(tenant_prefix):
        return mapped
    if tenant_prefix == "plo_":
        return "plo_opener"
    return f"{tenant_prefix}opener"


def build_flow_graph(flow_id: str, flow_set: FlowSet) -> dict[str, Any]:
    flow = flow_set.flows.get(flow_id)
    if flow is None:
        return {}
    known = {step_id(s, i): s for i, s in enumerate(flow.steps)}
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    extra_ids: set[str] = set()

    def _ensure_end() -> None:
        extra_ids.add("end")

    def _ensure_flow_hop(name: str) -> str:
        nid = f"flow:{name}"
        extra_ids.add(nid)
        return nid

    for i, step in enumerate(flow.steps):
        sid = step_id(step, i)
        kind = step_kind(step)
        nodes.append(
            {
                "id": sid,
                "kind": kind,
                "text": _preview(step, flow_set),
                "reply_id": step.utter,
                "slot": step.collect,
                "action": step.action,
                "index": i,
            }
        )
        if step.decide:
            for dest, label in _branch_targets(step.decide):
                if dest == "end":
                    _ensure_end()
                tgt = dest if dest in known or dest == "end" else dest
                if tgt not in known and tgt != "end" and tgt in flow_set.flows:
                    tgt = _ensure_flow_hop(tgt)
                    edges.append(
                        {
                            "from": sid,
                            "to": tgt,
                            "kind": "start_flow",
                            "label": label,
                        }
                    )
                    continue
                if tgt == "end":
                    _ensure_end()
                edges.append(
                    {"from": sid, "to": tgt, "kind": "decide", "label": label}
                )
        nxt = step.next
        if isinstance(nxt, str) and nxt.strip():
            dest = nxt.strip()
            if dest == "end":
                _ensure_end()
                edges.append({"from": sid, "to": "end", "kind": "next", "label": ""})
            elif dest in known:
                edges.append({"from": sid, "to": dest, "kind": "next", "label": ""})
            elif dest in flow_set.flows:
                hop = _ensure_flow_hop(dest)
                edges.append(
                    {"from": sid, "to": hop, "kind": "start_flow", "label": dest}
                )
            else:
                edges.append({"from": sid, "to": dest, "kind": "next", "label": ""})
        elif isinstance(nxt, list):
            for dest, label in _branch_targets(nxt):
                if dest == "end":
                    _ensure_end()
                edges.append(
                    {"from": sid, "to": dest, "kind": "decide", "label": label}
                )
        if step.escalate_to:
            dest = step.escalate_to.strip()
            if dest == "end":
                _ensure_end()
            edges.append(
                {
                    "from": sid,
                    "to": dest,
                    "kind": "escalate_to",
                    "label": "escalate",
                }
            )
        chain = _CHAIN_TO_FLOW.get(str(step.action or "").strip())
        if chain:
            hop = _ensure_flow_hop(chain)
            edges.append(
                {
                    "from": sid,
                    "to": hop,
                    "kind": "start_flow",
                    "label": chain,
                }
            )

    if "end" in extra_ids:
        nodes.append(
            {
                "id": "end",
                "kind": "action",
                "text": "end",
                "reply_id": None,
                "slot": None,
                "action": "end",
                "index": -1,
            }
        )
    for nid in sorted(extra_ids):
        if nid == "end" or any(n["id"] == nid for n in nodes):
            continue
        name = nid.split(":", 1)[-1]
        nodes.append(
            {
                "id": nid,
                "kind": "action",
                "text": f"start {name}",
                "reply_id": None,
                "slot": None,
                "action": "start_flow",
                "index": -1,
                "target_flow": name,
            }
        )
    return {
        "flow_id": flow_id,
        "description": flow.description.strip(),
        "scenarios": list(flow.scenarios or []),
        "nodes": nodes,
        "edges": edges,
    }


def live_position(state: ConversationState | None, flow_set: FlowSet) -> dict[str, Any]:
    if state is None:
        return {
            "flow_stack": [],
            "current_flow": None,
            "current_step_id": None,
            "awaited_slot": None,
        }
    stack: list[str] = []
    current_step_id: str | None = None
    awaited: str | None = None
    current_flow: str | None = None
    for frame in state.flow_stack:
        stack.append(frame.flow)
        current_flow = frame.flow
        flow = flow_set.flows.get(frame.flow)
        if flow is None or frame.step_index >= len(flow.steps):
            current_step_id = None
            awaited = None
            continue
        step = flow.steps[frame.step_index]
        current_step_id = step_id(step, frame.step_index)
        awaited = step.collect or None
    return {
        "flow_stack": stack,
        "current_flow": current_flow,
        "current_step_id": current_step_id,
        "awaited_slot": awaited,
    }
