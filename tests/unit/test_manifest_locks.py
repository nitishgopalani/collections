"""Regression tests for reply manifest mandatory-lock derivation."""

import importlib.util
import sys
from pathlib import Path

from app.flows.loader import load_all_flows
from app.flows.manifest import load_reply_manifest
from app.flows.overrides import validate_pack
from app.schemas.overrides import BrandOverridePack, BrandVariant, ReplyOverride

ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = ROOT / "scripts" / "generate_reply_manifest.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("generate_reply_manifest", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["generate_reply_manifest"] = module
    spec.loader.exec_module(module)
    return module


gen = _load_generator()


def test_locked_priority_reachable_replies_are_mandatory():
    flow_set = load_all_flows()
    manifest = load_reply_manifest()
    mandatory_expected = gen.mandatory_reply_ids_from_flows(flow_set)

    for reply_id in sorted(mandatory_expected):
        entry = manifest.entries.get(reply_id)
        assert entry is not None, f"missing manifest entry for reachable reply_id {reply_id!r}"
        assert (
            entry.is_mandatory
        ), f"{reply_id!r} reachable from locked-priority flow but not mandatory"

    dispute_locked = {
        rid
        for rid in mandatory_expected
        if rid.startswith(
            (
                "dispute",
                "amount_",
                "loan_closed",
                "not_due",
                "nach_",
                "double_",
                "payment_already",
                "ask_dispute",
            )
        )
    }
    assert len(dispute_locked) >= 16


def test_dispute_handoff_cannot_be_disabled():
    manifest = load_reply_manifest()
    assert manifest.entries["dispute_handoff"].is_mandatory is True

    pack = BrandOverridePack(
        agent_id="agent-test",
        pack_id="pack-test",
        manifest_version=manifest.version,
        overrides=[
            ReplyOverride(
                reply_id="dispute_handoff",
                enabled=False,
                variants=[BrandVariant(text="custom handoff copy")],
            )
        ],
    )
    errors = validate_pack(pack, manifest)
    assert any(
        error.code == "locked_disable" and error.reply_id == "dispute_handoff" for error in errors
    )


def test_committed_manifest_matches_generator():
    generated = gen.build_manifest_dict(load_all_flows())
    committed = load_reply_manifest().model_dump(mode="json")
    assert generated == committed
