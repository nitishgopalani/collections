#!/usr/bin/env python3
"""Generate reply_manifest.json — slots + mandatory locks from flow-priority reachability."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.engine.nlg import COLLECT_SLOT_REPLY_IDS, _SLOT_PATTERN
from app.flows.loader import FLOWS_DIR, load_all_flows
from app.flows.manifest import MANIFEST_VERSION, REPLY_MANIFEST_PATH
from app.schemas.flow import FlowSet

LOCKED_PRIORITIES = frozenset({"identity", "opt_out", "vulnerable", "dispute"})


def _slots_in_text(text: str) -> list[str]:
    return sorted(set(_SLOT_PATTERN.findall(text)))


def mandatory_reply_ids_from_flows(flow_set: FlowSet) -> set[str]:
    """Reply ids reachable (utter or collect→COLLECT_SLOT_REPLY_IDS) from locked-priority flows."""
    mandatory: set[str] = set()
    for flow in flow_set.flows.values():
        if flow.priority not in LOCKED_PRIORITIES:
            continue
        for step in flow.steps:
            if step.utter:
                mandatory.add(step.utter)
            if step.collect:
                reply_id = COLLECT_SLOT_REPLY_IDS.get(step.collect)
                if reply_id:
                    mandatory.add(reply_id)
    return mandatory


def build_manifest_dict(flow_set: FlowSet | None = None) -> dict[str, object]:
    resolved = flow_set or load_all_flows()
    mandatory = mandatory_reply_ids_from_flows(resolved)
    entries: dict[str, dict[str, object]] = {}
    for reply_id, variants in sorted(resolved.responses.items()):
        slots: set[str] = set()
        for variant in variants:
            slots.update(_slots_in_text(variant.text))
        entries[reply_id] = {
            "slots": sorted(slots),
            "is_mandatory": reply_id in mandatory,
        }
    return {"version": MANIFEST_VERSION, "entries": entries}


def write_manifest(
    path: Path = REPLY_MANIFEST_PATH, *, flows_dir: Path = FLOWS_DIR
) -> dict[str, object]:
    # Production manifest never includes fabricated test_* tenants.
    import os

    os.environ["COLLECTIONS_INCLUDE_TEST_FLOWS"] = "false"
    flow_set = load_all_flows(flows_dir)
    manifest = build_manifest_dict(flow_set)
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest


def check_manifest(path: Path = REPLY_MANIFEST_PATH, *, flows_dir: Path = FLOWS_DIR) -> int:
    """Regenerate and diff against committed manifest; exit code 1 on drift."""
    import os

    os.environ["COLLECTIONS_INCLUDE_TEST_FLOWS"] = "false"
    generated = build_manifest_dict(load_all_flows(flows_dir))
    if not path.is_file():
        print(f"Missing committed manifest: {path}", file=sys.stderr)
        return 1
    committed = json.loads(path.read_text(encoding="utf-8"))

    if generated == committed:
        mandatory_count = sum(1 for entry in generated["entries"].values() if entry["is_mandatory"])
        print(
            f"reply manifest OK ({len(generated['entries'])} entries, {mandatory_count} mandatory)"
        )
        return 0

    gen_mandatory = {rid for rid, entry in generated["entries"].items() if entry["is_mandatory"]}
    com_mandatory = {rid for rid, entry in committed["entries"].items() if entry["is_mandatory"]}
    print("reply manifest drift detected", file=sys.stderr)
    only_gen = sorted(gen_mandatory - com_mandatory)
    only_com = sorted(com_mandatory - gen_mandatory)
    if only_gen:
        print(
            f"  newly mandatory ({len(only_gen)}): {only_gen[:10]}{'...' if len(only_gen) > 10 else ''}"
        )
    if only_com:
        print(
            f"  no longer mandatory ({len(only_com)}): {only_com[:10]}{'...' if len(only_com) > 10 else ''}"
        )
    slot_diffs = [
        rid
        for rid in sorted(set(generated["entries"]) | set(committed["entries"]))
        if generated["entries"].get(rid) != committed["entries"].get(rid)
    ]
    if slot_diffs:
        print(
            f"  entries changed ({len(slot_diffs)}): {slot_diffs[:10]}{'...' if len(slot_diffs) > 10 else ''}"
        )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate or verify reply_manifest.json")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify committed manifest matches regeneration (exit 1 on drift)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPLY_MANIFEST_PATH,
        help="Output path for generated manifest",
    )
    parser.add_argument(
        "--flows-dir",
        type=Path,
        default=FLOWS_DIR,
        help="Directory containing flow YAML files",
    )
    args = parser.parse_args()

    if args.check:
        return check_manifest(args.output, flows_dir=args.flows_dir)

    manifest = write_manifest(args.output, flows_dir=args.flows_dir)
    mandatory_count = sum(1 for entry in manifest["entries"].values() if entry["is_mandatory"])
    print(
        f"Wrote {args.output} " f"({len(manifest['entries'])} entries, {mandatory_count} mandatory)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
