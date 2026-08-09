"""DT R2 — verify-then-delete the 41 candidate orphan flows from TENANCY_AUDIT §4.3.

A flow is DELETABLE iff it has ZERO references in ALL of:
  1. reply_manifest.json  (flow name appears as a key or value)
  2. tests/               (flow name appears in any test file)
  3. force_flow-alias map (app/ws/routing.py FORCE_FLOW_ALIASES + app/ws/handler.py)

Anything ambiguous stays, marked with an ORPHAN(date) comment for the next sweep.

Run: python scripts/_dt_orphan_verify.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLOWS_DIR = ROOT / "app" / "flows"
MANIFEST = ROOT / "app" / "engine" / "reply_manifest.json"
TESTS_DIR = ROOT / "tests"
ROUTING = ROOT / "app" / "ws" / "routing.py"
HANDLER = ROOT / "app" / "ws" / "handler.py"

CANDIDATES = [
    # SOT (12 listed in audit; 14 here — 2 pre-existing test-only, verify)
    "sot_obj_amount_in_2_days", "sot_obj_cant_login", "sot_obj_credit_manager",
    "sot_obj_is_bot", "sot_obj_month_only", "sot_obj_pay_later_penalty",
    "sot_obj_penalty_now", "sot_obj_pending_status", "sot_obj_processing_fee",
    "sot_obj_reduce_amount", "sot_obj_references_called", "sot_obj_support_number",
    "sot_obj_total_payable", "sot_obj_unknown_query",
    # PLO (24)
    "plo_obj_assurance_pd", "plo_obj_dealer_pay", "plo_obj_dealer_pay_pd",
    "plo_obj_death_pd", "plo_obj_lost_qr", "plo_obj_multiple_loans",
    "plo_obj_multiple_loans_pd", "plo_obj_npa_angry", "plo_obj_npa_assurance",
    "plo_obj_npa_branch_address", "plo_obj_npa_death", "plo_obj_npa_hardship",
    "plo_obj_npa_lost_qr", "plo_obj_npa_medical", "plo_obj_npa_paid_official",
    "plo_obj_npa_refuse", "plo_obj_npa_where_to_pay", "plo_obj_npa_wrong_number",
    "plo_obj_paid_official_pd", "plo_obj_personal_issue", "plo_obj_where_to_pay",
    "plo_obj_will_not_pay", "plo_obj_will_you_pay", "plo_obj_will_you_pay_pd",
    # Generic (3)
    "beyond_authority", "incapacitated_borrower", "off_topic_redirect",
]


def _load_manifest_text() -> str:
    if not MANIFEST.is_file():
        return ""
    return MANIFEST.read_text(encoding="utf-8")


def _load_tests_text() -> str:
    chunks: list[str] = []
    for p in TESTS_DIR.rglob("*"):
        if p.is_file() and p.suffix in {".py", ".json", ".txt", ".yml", ".yaml"}:
            try:
                chunks.append(p.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, OSError):
                pass
    return "\n".join(chunks)


def _load_alias_text() -> str:
    chunks: list[str] = []
    for p in (ROUTING, HANDLER):
        if p.is_file():
            chunks.append(p.read_text(encoding="utf-8"))
    return "\n".join(chunks)


def _flow_yaml_files() -> dict[str, Path]:
    """Map flow_id -> the YAML file that defines it."""
    out: dict[str, Path] = {}
    for yml in FLOWS_DIR.rglob("*.yml"):
        try:
            raw = yml.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        # Match "  <flow_id>:" at the flow-key indent (2 spaces under flows:).
        for m in re.finditer(r"^  ([a-z_][a-z0-9_]*):\s*$", raw, re.MULTILINE):
            out[m.group(1)] = yml
    return out


def main() -> None:
    manifest = _load_manifest_text()
    tests = _load_tests_text()
    aliases = _load_alias_text()
    flow_files = _flow_yaml_files()

    deletable: list[str] = []
    ambiguous: list[str] = []
    not_defined: list[str] = []

    for flow in CANDIDATES:
        # Word-bounded search so "sot_obj_pay" doesn't match "sot_obj_payment".
        pat = re.compile(r"\b" + re.escape(flow) + r"\b")
        in_manifest = bool(pat.search(manifest))
        in_tests = bool(pat.search(tests))
        in_alias = bool(pat.search(aliases))
        defined = flow in flow_files

        if not defined:
            not_defined.append(flow)
            continue

        if not in_manifest and not in_tests and not in_alias:
            deletable.append(flow)
        else:
            reasons = []
            if in_manifest:
                reasons.append("manifest")
            if in_tests:
                reasons.append("tests")
            if in_alias:
                reasons.append("alias")
            ambiguous.append((flow, "+".join(reasons), flow_files[flow]))

    print("=" * 72)
    print(f"DELETABLE ({len(deletable)}):")
    for f in sorted(deletable):
        print(f"  {f}  [{flow_files[f].name}]")
    print()
    print(f"AMBIGUOUS (keep + ORPHAN comment) ({len(ambiguous)}):")
    for f, reasons, path in sorted(ambiguous, key=lambda x: x[0]):
        print(f"  {f}  refs={reasons}  [{path.name}]")
    print()
    if not_defined:
        print(f"NOT DEFINED in any flow YAML ({len(not_defined)}):")
        for f in sorted(not_defined):
            print(f"  {f}")
    print()
    print(f"summary: deletable={len(deletable)} ambiguous={len(ambiguous)} not_defined={len(not_defined)}")


if __name__ == "__main__":
    main()
