import logging
import os
from pathlib import Path

import yaml

from app.flows.validator import validate_flow_set
from app.schemas.flow import Flow, FlowSet, ResponseTemplate

logger = logging.getLogger(__name__)

FLOWS_DIR = Path(__file__).resolve().parent

# Fixture tenants (e.g. test_generic) stay out of production flow loads / manifests
# unless explicitly opted in (pytest conftest sets this).
_TEST_FLOW_DIR_NAMES = frozenset({"test_generic"})


def include_test_flows() -> bool:
    return os.environ.get("COLLECTIONS_INCLUDE_TEST_FLOWS", "").lower() in {
        "1",
        "true",
        "yes",
    }


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
    allow_test = include_test_flows()
    for path in sorted(flows_dir.glob("*/*.yml")):
        if path.parent.name in _TEST_FLOW_DIR_NAMES and not allow_test:
            continue
        partial = load_flow_yaml(path)
        merged_flows.update(partial.flows)
        merged_responses.update(partial.responses)
    validate_flow_set(merged_flows)
    return FlowSet(flows=merged_flows, responses=merged_responses)


# Cached singleton — readers see a stable FlowSet reference; reload_flow_set() reassigns
# atomically under the CPython GIL (no lock needed for read-mostly access).
_FLOW_SET_CACHE: FlowSet | None = None


def get_flow_set(*, flows_dir: Path = FLOWS_DIR) -> FlowSet:
    """Return the cached flow set, building once via load_all_flows on first call."""
    global _FLOW_SET_CACHE
    if _FLOW_SET_CACHE is None:
        _FLOW_SET_CACHE = load_all_flows(flows_dir)
    return _FLOW_SET_CACHE


def reload_flow_set(*, flows_dir: Path = FLOWS_DIR) -> FlowSet:
    """Rebuild the flow set and replace the module cache (hot-reload seam)."""
    global _FLOW_SET_CACHE
    _FLOW_SET_CACHE = load_all_flows(flows_dir)
    # Invalidate Tier-2 tenant catalogs keyed by (tenant_id, flow_set_version).
    from app.engine.catalog import bump_flow_set_version

    bump_flow_set_version()
    return _FLOW_SET_CACHE
