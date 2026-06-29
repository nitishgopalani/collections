"""Salary On Time flow pack loading and wiring."""

from app.flows.loader import reload_flow_set
from app.ws.routing import AGENT_FORCE_FLOW_MAP, resolve_agent_routing


def test_sot_flow_pack_loads():
    flows = reload_flow_set()
    for name in (
        "sot_opener",
        "sot_offer_pre_closure",
        "sot_push",
        "sot_commit",
        "sot_close",
        "sot_obj_already_paid_q",
        "sot_obj_busy",
        "sot_obj_high_interest",
        "sot_obj_never_loan",
        "sot_obj_medical",
        "sot_obj_link_request",
        "sot_obj_enhanced_amount",
        "sot_obj_penalty",
        "sot_obj_wont_pay",
    ):
        assert name in flows.flows, f"missing flow {name}"
    assert "sot_greeting" in flows.responses
    assert flows.responses["sot_offer_pre_closure"][0].language == "hi"


def test_salary_on_time_agent_routing():
    force_flow, tenant_id = resolve_agent_routing("salary-on-time-test")
    assert force_flow == "sot_opener"
    assert tenant_id == "salary_on_time"
    assert AGENT_FORCE_FLOW_MAP["salary-on-time"] == ("sot_opener", "salary_on_time")
