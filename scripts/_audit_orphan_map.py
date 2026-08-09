"""Orphan-map probe for DT refactor plan (audit only)."""
from __future__ import annotations
import json, os, re, yaml, sys

ROOT = r"c:\Users\nitis\source\repos\Main\Collection\app"
FLOWS_DIR = os.path.join(ROOT, "flows")
TENANTS_DIR = os.path.join(ROOT, "tenants")

# 1. All defined flow names (from every YAML under flows/)
defined_flows: set[str] = set()
referenced: set[str] = set()
for dirpath, _dirs, files in os.walk(FLOWS_DIR):
    for f in files:
        if not f.endswith((".yml", ".yaml")):
            continue
        path = os.path.join(dirpath, f)
        with open(path, encoding="utf-8") as fh:
            try:
                data = yaml.safe_load(fh)
            except Exception as e:
                print(f"YAML parse error {path}: {e}", file=sys.stderr)
                continue
        if not isinstance(data, dict):
            continue
        for fid, flow in (data.get("flows", {}) or {}).items():
            if isinstance(fid, str):
                defined_flows.add(fid)
            if isinstance(flow, dict):
                for step in flow.get("steps", []) or []:
                    if not isinstance(step, dict):
                        continue
                    if step.get("action") == "start_flow" and step.get("flow"):
                        referenced.add(str(step["flow"]))
                    nxt = step.get("next")
                    if isinstance(nxt, str):
                        referenced.add(nxt)

# 2. All referenced flow names (from tenant YAML + coercion_chain + flow steps)
for f in os.listdir(TENANTS_DIR):
    if not f.endswith((".yml", ".yaml")):
        continue
    with open(os.path.join(TENANTS_DIR, f), encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    for key in ("onrails_flows", "on_rails_flows", "pinned_flows",
                "dispute_flows", "deflection_objections", "blocked_commands"):
        for v in data.get(key, []) or []:
            if isinstance(v, str):
                referenced.add(v)
    for key in ("callback_flow", "reversal_target_flow"):
        v = data.get(key, "")
        if isinstance(v, str) and v:
            referenced.add(v)
    chain = data.get("coercion_chain", []) or []
    # coercion_chain entries are stage names (dispute/willing/...), not flows — skip
    # dispute_theme_flows
    for v in (data.get("dispute_theme_flows", {}) or {}).values():
        if isinstance(v, str):
            referenced.add(v)

# 3. Flows referenced inside flow YAML (start_flow / escalate_to / next targets) — handled in pass 1 above

# 4. Also count flows referenced by tests (golden) — quick grep
import subprocess
test_refs: set[str] = set()
for root, _dirs, files in os.walk(os.path.join(ROOT, "..", "tests")):
    for f in files:
        if not f.endswith(".py"):
            continue
        path = os.path.join(root, f)
        try:
            txt = open(path, encoding="utf-8").read()
        except Exception:
            continue
        for name in defined_flows:
            if name in txt:
                test_refs.add(name)

orphans = defined_flows - referenced - test_refs
print(f"defined_flows: {len(defined_flows)}")
print(f"referenced (yaml): {len(referenced)}")
print(f"referenced (tests): {len(test_refs)}")
print(f"orphans (defined - referenced - test_refs): {len(orphans)}")
for o in sorted(orphans):
    print(f"  ORPHAN: {o}")
