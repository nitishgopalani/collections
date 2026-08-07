"""P2.1 — tenant flow catalog builder + cache invalidation."""

import json

from app.engine.catalog import (
    bump_flow_set_version,
    clear_catalog_cache,
    compress_flow_description,
    estimate_catalog_tokens,
    filter_deflection_objections,
    get_flow_set_version,
    tenant_flow_catalog,
)
from app.engine.tenant_profile import clear_tenant_profile_cache, get_tenant_profile
from app.flows.loader import get_flow_set, reload_flow_set
from app.schemas.flow import Flow, FlowSet


def test_compress_flow_description_first_sentence_and_triggers():
    raw = (
        "Borrower asks how to pay online. More detail here. "
        "Triggers: kaise pay karun, link bhejo, payment kaise kare, "
        "how do I pay, send me the link please right now urgently."
    )
    out = compress_flow_description(raw, max_words=25)
    assert out.startswith("Borrower asks how to pay online.")
    assert "Triggers:" in out
    # Trigger side truncated to ~25 words
    trig = out.split("Triggers:", 1)[1].strip()
    assert len(trig.split()) <= 25


def test_tenant_flow_catalog_sot_has_no_scores_and_prefix_only():
    clear_tenant_profile_cache()
    clear_catalog_cache()
    profile = get_tenant_profile("salary_on_time")
    assert profile is not None
    flows = get_flow_set()
    catalog = tenant_flow_catalog(profile, flows)
    assert len(catalog) >= 40
    assert all("score" not in c for c in catalog)
    names = {c["name"] for c in catalog}
    assert "sot_obj_link_request" in names
    assert "sot_obj_never_loan" in names
    assert "sot_obj_busy" in names
    # on-due / post-due packs (prefix does not start with "sot_")
    assert "sotod_offer" in names
    assert "sotpd_push" in names
    tokens = estimate_catalog_tokens(catalog)
    assert tokens <= 4500, f"SOT catalog tokens {tokens} exceed 4.5k budget"
    # Stable JSON shape for prompt.
    assert json.loads(json.dumps(catalog))


def test_catalog_cache_invalidates_on_reload_flow_set():
    clear_tenant_profile_cache()
    clear_catalog_cache()
    profile = get_tenant_profile("salary_on_time")
    assert profile is not None
    flows = get_flow_set()
    v0 = get_flow_set_version()
    c1 = tenant_flow_catalog(profile, flows)
    assert tenant_flow_catalog(profile, flows) == c1  # cache hit

    # Mutate a description in a private FlowSet and bump via reload path.
    # reload_flow_set rebuilds from disk (same content) but must bump version
    # so a subsequent catalog build is not served from the stale key.
    v_before = get_flow_set_version()
    reload_flow_set()
    v_after = get_flow_set_version()
    assert v_after == v_before + 1
    assert v_after != v0 or v_before == v0  # version advanced

    # Stale-cache proof: seed cache under old version key with a poison entry,
    # bump, and confirm the poison is not returned.
    clear_catalog_cache()
    from app.engine import catalog as cat

    poison = [{"name": "sot_POISON", "description": "stale"}]
    cat._CATALOG_CACHE[(profile.tenant_id, get_flow_set_version())] = poison
    assert tenant_flow_catalog(profile, get_flow_set())[0]["name"] == "sot_POISON"
    bump_flow_set_version()
    fresh = tenant_flow_catalog(profile, get_flow_set())
    assert fresh[0]["name"] != "sot_POISON"
    assert "sot_obj_link_request" in {c["name"] for c in fresh}


def test_filter_deflection_objections_keeps_disputes_and_info():
    clear_tenant_profile_cache()
    profile = get_tenant_profile("salary_on_time")
    assert profile is not None
    catalog = [
        {"name": "sot_obj_busy", "description": "busy"},
        {"name": "sot_obj_never_loan", "description": "dispute"},
        {"name": "sot_obj_link_request", "description": "info"},
        {"name": "sot_obj_hold", "description": "hold"},
    ]
    filtered = filter_deflection_objections(catalog, profile)
    names = {c["name"] for c in filtered}
    assert "sot_obj_busy" not in names
    assert "sot_obj_hold" not in names
    assert "sot_obj_never_loan" in names
    assert "sot_obj_link_request" in names


def test_compress_on_minimal_flow_set():
    flows = FlowSet(
        flows={
            "sot_opener": Flow(
                description="Greeting only.",
                priority="identity",
                steps=[],
            )
        },
        responses={},
    )
    from app.engine.tenant_profile import TenantRuntimeProfile

    profile = TenantRuntimeProfile(tenant_id="t", flow_prefix="sot_")
    clear_catalog_cache()
    bump_flow_set_version()
    cat = tenant_flow_catalog(profile, flows)
    assert cat == [{"name": "sot_opener", "description": "Greeting only."}]
