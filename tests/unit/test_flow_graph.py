"""UI-6A flow graph builder."""

from app.admin.flow_graph import build_flow_graph, live_position, tenant_catalog
from app.engine.tenant_profile import get_tenant_profile
from app.flows.loader import get_flow_set
from app.schemas.state import ConversationState, Frame


def test_predue_graph_has_collect_decide_and_kinds():
    graph = build_flow_graph("plo_predue", get_flow_set())
    kinds = {n["id"]: n["kind"] for n in graph["nodes"]}
    assert kinds["wait_intent"] == "collect"
    assert kinds["route_intent"] == "decide"
    assert kinds["greet_detail"] == "utter"
    assert kinds["do_hangup"] == "action"
    edges = {(e["from"], e["to"], e["kind"]) for e in graph["edges"]}
    assert ("wait_intent", "route_intent", "next") in edges
    assert any(e["from"] == "route_intent" and e["kind"] == "decide" for e in graph["edges"])
    wait = next(n for n in graph["nodes"] if n["id"] == "wait_intent")
    assert wait["slot"] == "plo_payment_intent"
    greet = next(n for n in graph["nodes"] if n["id"] == "greet_detail")
    assert greet["reply_id"] == "plo_predue_greeting"
    assert greet["text"]


def test_opener_chain_emits_start_flow_edge():
    graph = build_flow_graph("plo_opener", get_flow_set())
    hops = [e for e in graph["edges"] if e["kind"] == "start_flow"]
    assert any(e["to"] == "flow:plo_predue" for e in hops)
    assert any(n["id"] == "flow:plo_predue" for n in graph["nodes"])


def test_npa_collect_has_escalate_edge():
    graph = build_flow_graph("plo_npa", get_flow_set())
    esc = [e for e in graph["edges"] if e["kind"] == "escalate_to"]
    assert esc


def test_paisalo_catalog_includes_opener_and_ladders():
    prof = get_tenant_profile("paisalo")
    assert prof is not None
    cat = tenant_catalog(prof, get_flow_set())
    ids = {r["id"] for r in cat}
    assert "plo_opener" in ids
    assert "plo_predue" in ids
    assert "plo_npa" in ids
    predue_only = {r["id"] for r in tenant_catalog(prof, get_flow_set(), scenario="predue")}
    assert "plo_predue" in predue_only
    assert "plo_opener" in predue_only


def test_health_marks_implicit_repair_not_missing_escalate():
    from app.admin.flow_health import annotate_graph_health, attach_system_rail

    flows = get_flow_set()
    graph = build_flow_graph("plo_predue", flows)
    attach_system_rail(graph)
    annotate_graph_health(graph, flows, catalog_ids={"plo_predue", "plo_opener"})
    wait = next(n for n in graph["nodes"] if n["id"] == "wait_intent")
    codes = {r["code"] for r in wait["health"]["reasons"]}
    assert "collect_implicit_repair" in codes
    assert "collect_no_escalate" not in codes
    assert wait["health"]["level"] == "warning"
    assert graph["health"]["errors"] == 0
    assert any(n["kind"] == "system_rail" for n in graph["nodes"])
    opener = build_flow_graph("plo_opener", flows)
    hop = next(n for n in opener["nodes"] if n.get("target_flow") == "plo_predue")
    assert hop["kind"] == "flow_ref"


def test_health_red_when_implicit_repair_unreachable():
    from app.admin.flow_health import annotate_graph_health, attach_system_rail

    flows = get_flow_set()
    graph = build_flow_graph("plo_predue", flows)
    attach_system_rail(graph)
    annotate_graph_health(
        graph,
        flows,
        catalog_ids={"plo_predue"},
        repair_reachable=False,
    )
    wait = next(n for n in graph["nodes"] if n["id"] == "wait_intent")
    codes = {r["code"] for r in wait["health"]["reasons"]}
    assert "collect_no_escalate" in codes
    assert wait["health"]["level"] == "error"


def test_live_position_reads_top_frame():
    flows = get_flow_set()
    predue = flows.flows["plo_predue"]
    idx = next(i for i, s in enumerate(predue.steps) if s.id == "wait_intent")
    state = ConversationState(
        call_id="g",
        tenant_id="paisalo",
        borrower_id="b",
        flow_stack=[
            Frame(flow="plo_opener", step_index=0),
            Frame(flow="plo_predue", step_index=idx),
        ],
    )
    pos = live_position(state, flows)
    assert pos["flow_stack"] == ["plo_opener", "plo_predue"]
    assert pos["current_flow"] == "plo_predue"
    assert pos["current_step_id"] == "wait_intent"
    assert pos["awaited_slot"] == "plo_payment_intent"
