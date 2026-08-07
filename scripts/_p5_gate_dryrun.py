"""P5 Gate dry-run: scan PaisaLo responses; derive Hindi-family allowlist (no emi).

Usage:
    cd Collection
    py -3 scripts/_p5_gate_dryrun.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from app.compliance_defaults import COLLECTION_PRESSURE_PHRASES
from app.config import tenant_config
from app.engine.compliance_rules import (
    evaluate_pressure_with_allowlist,
    find_substring_spans,
    matches_any,
    normalize,
)

FLOWS_DIR = Path(__file__).resolve().parent.parent / "app" / "flows" / "paisalo"
SOT_FLOWS_DIR = Path(__file__).resolve().parent.parent / "app" / "flows" / "salary_on_time"
OUTPUT_PATH = Path(__file__).resolve().parent / "_p5_gate_hits.txt"

# Hindi legal/NPA family added in P5.4 (emi intentionally excluded from allowlist).
HINDI_FAMILY = {
    "कानूनी कार्रवाई",
    "कानूनी कार्यवाही",
    "कानूनी कारवाही",
    "सख़्त कार्रवाई",
    "सख्त कार्रवाई",
    "सख़्त कार्यवाही",
    "कानूनी परिणाम",
    "कानूनी प्रक्रिया",
    "NPA घोषित",
    "खाता NPA",
    "डिफॉल्ट हो सकता",
    "डिफ़ॉल्ट का सामना",
}


def _load_responses(flows_dir: Path) -> dict[str, list[dict]]:
    responses: dict[str, list[dict]] = {}
    if not flows_dir.is_dir():
        return responses
    for yml_path in sorted(flows_dir.glob("*.yml")):
        with yml_path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        if not isinstance(raw, dict):
            continue
        for rid, variants in raw.get("responses", {}).items():
            if isinstance(variants, list):
                responses[rid] = variants
    return responses


def run_gate_dryrun() -> list[str]:
    cfg = tenant_config("default")
    # Prefer live defaults tuple (includes P5.4 Hindi family).
    pressure_phrases = list(COLLECTION_PRESSURE_PHRASES)
    prohibited = cfg.prohibited_outbound_phrases
    allowlist: list[str] = []

    responses = _load_responses(FLOWS_DIR)
    # Prefer live paisalo allowlist when present (CP5 F1 dry-run with exemptions).
    from app.engine.tenant_profile import clear_tenant_profile_cache, get_tenant_profile

    clear_tenant_profile_cache()
    plo_profile = get_tenant_profile("paisalo")
    live_allowlist = list((plo_profile.gate_allowlisted_phrases if plo_profile else []) or [])
    # Empty-allowlist scan (derive) + live-allowlist scan (F1 verify).
    hits: list[str] = []
    family_hit_phrases: set[str] = set()
    out_of_family: list[str] = []
    allowlisted_warning_rows = 0
    residual_blocks = 0

    for response_id, variants in sorted(responses.items()):
        for variant in variants:
            text = variant.get("text", "")
            if not text:
                continue
            attempt = variant.get("attempt", "")
            norm = normalize(text)
            for fam in HINDI_FAMILY:
                if find_substring_spans(norm, fam):
                    family_hit_phrases.add(fam)
            blocking_empty, _ = evaluate_pressure_with_allowlist(
                text, pressure_phrases, allowlist
            )
            blocking_live, warnings_live = evaluate_pressure_with_allowlist(
                text, pressure_phrases, live_allowlist
            )
            prohibited_hit = matches_any(text, prohibited)
            if not blocking_empty and not blocking_live and not warnings_live and not prohibited_hit:
                continue
            label = f"{response_id}"
            if attempt:
                label += f" (attempt={attempt})"
            hits.append(f"--- {label} ---")
            if blocking_empty:
                hits.append(f"  BLOCKED (empty allowlist): {blocking_empty}")
                if blocking_empty not in HINDI_FAMILY:
                    out_of_family.append(f"{label} :: {blocking_empty}")
            if blocking_live:
                residual_blocks += 1
                hits.append(f"  BLOCKED (paisalo allowlist): {blocking_live}")
            if warnings_live:
                allowlisted_warning_rows += 1
                for w in warnings_live:
                    hits.append(f"  ALLOWLISTED_WARNING: {w}")
            for fam in pressure_phrases:
                if fam in HINDI_FAMILY and find_substring_spans(norm, fam):
                    hits.append(f"  FAMILY_HIT: {fam}")
            if prohibited_hit:
                hits.append(f"  BLOCKED prohibited: {prohibited_hit}")
                out_of_family.append(f"{label} :: prohibited:{prohibited_hit}")
            hits.append(f"  text: {text[:160]}...")
            hits.append("")

    derived = [p for p in pressure_phrases if p in family_hit_phrases]

    # SOT regression scan — new Hindi family hits on SOT copy.
    sot_family_hits: list[str] = []
    for response_id, variants in sorted(_load_responses(SOT_FLOWS_DIR).items()):
        for variant in variants:
            text = variant.get("text", "")
            if not text:
                continue
            for phrase in HINDI_FAMILY:
                blocking, _ = evaluate_pressure_with_allowlist(
                    text, [phrase], []
                )
                if blocking:
                    sot_family_hits.append(f"{response_id} :: {blocking}")

    n_hits = len([h for h in hits if h.startswith("---")])
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        f.write("P5 PaisaLo Gate Dry-Run Results (P5.4 Hindi lexicon)\n")
        f.write(f"Total responses scanned: {sum(len(v) for v in responses.values())}\n")
        f.write(f"Total hit rows: {n_hits}\n")
        f.write("=" * 60 + "\n\n")
        f.write("## Hindi-family pressure phrases that ACTUALLY hit (allowlist candidates)\n")
        for p in derived:
            f.write(f"  + {p}\n")
        if not derived:
            f.write("  (none)\n")
        f.write("\n## Derived gate_allowlisted_phrases (emi excluded)\n")
        f.write("gate_allowlisted_phrases:\n")
        for p in derived:
            f.write(f"  - {p!r}\n".replace("'", '"') if False else f'  - "{p}"\n')
        f.write("\n## Out-of-family hits under empty allowlist (NOT allowlisted)\n")
        for line in out_of_family:
            f.write(f"  {line}\n")
        if not out_of_family:
            f.write("  (none — emi removed from lexicon)\n")
        f.write(
            f"\n## With paisalo allowlist: allowlisted_warning_rows={allowlisted_warning_rows} "
            f"residual_blocks={residual_blocks}\n"
        )
        f.write("\n## SOT copy × Hindi family (expect zero)\n")
        if sot_family_hits:
            for line in sot_family_hits:
                f.write(f"  REGRESSION {line}\n")
        else:
            f.write("  (none — no new SOT hits)\n")
        f.write("\n## Raw hits\n")
        f.write("=" * 60 + "\n\n")
        f.write("\n".join(hits))

    print(f"Gate dry-run complete. {n_hits} hit rows.")
    print(f"  Hindi-family distinct phrases hit: {len(derived)}")
    for p in derived:
        print(f"    - {p}")
    print(f"  Out-of-family (empty allowlist): {len(out_of_family)}")
    print(
        f"  With paisalo allowlist: warnings_rows={allowlisted_warning_rows} "
        f"residual_blocks={residual_blocks}"
    )
    print(f"  SOT Hindi-family hits: {len(sot_family_hits)}")
    print(f"Results: {OUTPUT_PATH}")
    return derived


if __name__ == "__main__":
    run_gate_dryrun()
