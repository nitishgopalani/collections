from pathlib import Path

from app.flows.loader import load_all_flows, load_flow_yaml

FLOWS_DIR = Path(__file__).resolve().parents[2] / "app" / "flows"


def test_load_all_seed_flows():
    flow_set = load_all_flows(FLOWS_DIR)
    assert "pay_now" in flow_set.flows
    assert "promise_to_pay" in flow_set.flows
    assert "dispute" in flow_set.flows
    assert "vulnerability" in flow_set.flows
    assert "partial_payment" in flow_set.flows
    assert "already_initiated" in flow_set.flows
    assert "dues_breakup" in flow_set.flows
    assert "alt_channel" in flow_set.flows


def test_promise_to_pay_yaml():
    path = FLOWS_DIR / "promise_to_pay.yml"
    partial = load_flow_yaml(path)
    ptp = partial.flows["promise_to_pay"]
    assert ptp.priority == "ptp"
    assert ptp.steps[0].collect == "ptp_date"
    assert "confirm_ptp" in partial.responses
