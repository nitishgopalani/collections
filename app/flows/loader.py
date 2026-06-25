import logging
from pathlib import Path

import yaml

from app.flows.validator import validate_flow_set
from app.schemas.flow import Flow, FlowSet, ResponseTemplate

logger = logging.getLogger(__name__)

FLOWS_DIR = Path(__file__).resolve().parent


def load_flow_yaml(path: Path) -> FlowSet:
    """Load and parse a flow YAML file. Validation expanded in Sprint 2."""
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid flow file: {path}")
    flows_raw = raw.get("flows", {})
    responses_raw = raw.get("responses", {})
    flows = {name: Flow.model_validate(defn) for name, defn in flows_raw.items()}
    validate_flow_set(flows)
    responses: dict[str, list[ResponseTemplate]] = {}
    for rid, variants in responses_raw.items():
        responses[rid] = [ResponseTemplate.model_validate(v) for v in variants]
    return FlowSet(flows=flows, responses=responses)


def load_all_flows(flows_dir: Path = FLOWS_DIR) -> FlowSet:
    """Merge all YAML files under app/flows/. Sprint 2 adds full validation."""
    merged_flows: dict[str, Flow] = {}
    merged_responses: dict[str, list[ResponseTemplate]] = {}
    if not flows_dir.is_dir():
        return FlowSet(flows=merged_flows, responses=merged_responses)
    for path in sorted(flows_dir.glob("*.yml")):
        partial = load_flow_yaml(path)
        merged_flows.update(partial.flows)
        merged_responses.update(partial.responses)
    validate_flow_set(merged_flows)
    return FlowSet(flows=merged_flows, responses=merged_responses)
