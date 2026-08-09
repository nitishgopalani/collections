"""DT R2 — delete 31 confirmed orphan flows + tag 10 ambiguous with ORPHAN(date).

Deletable = zero manifest + zero test + zero alias refs (from _dt_orphan_verify.py).
Ambiguous = has alias ref (in FORCE_FLOW_ALIASES) → keep, prepend ORPHAN(2026-08-09) comment.

Removes the flow block from its YAML and the reply_manifest.json entry.
Idempotent: re-running on already-cleaned files is a no-op.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLOWS_DIR = ROOT / "app" / "flows"
MANIFEST = ROOT / "app" / "engine" / "reply_manifest.json"
DATE = "2026-08-09"

DELETABLE = {
    "beyond_authority", "incapacitated_borrower", "off_topic_redirect",
    "plo_obj_assurance_pd", "plo_obj_dealer_pay", "plo_obj_dealer_pay_pd",
    "plo_obj_death_pd", "plo_obj_lost_qr", "plo_obj_multiple_loans",
    "plo_obj_multiple_loans_pd", "plo_obj_npa_angry", "plo_obj_npa_assurance",
    "plo_obj_npa_branch_address", "plo_obj_npa_death", "plo_obj_npa_hardship",
    "plo_obj_npa_lost_qr", "plo_obj_npa_medical", "plo_obj_npa_paid_official",
    "plo_obj_npa_refuse", "plo_obj_npa_where_to_pay", "plo_obj_npa_wrong_number",
    "plo_obj_paid_official_pd", "plo_obj_personal_issue", "plo_obj_where_to_pay",
    "plo_obj_will_not_pay", "plo_obj_will_you_pay", "plo_obj_will_you_pay_pd",
    "sot_obj_amount_in_2_days", "sot_obj_pay_later_penalty",
    "sot_obj_penalty_now", "sot_obj_total_payable",
}

AMBIGUOUS = {
    "sot_obj_cant_login", "sot_obj_credit_manager", "sot_obj_is_bot",
    "sot_obj_month_only", "sot_obj_pending_status", "sot_obj_processing_fee",
    "sot_obj_reduce_amount", "sot_obj_references_called",
    "sot_obj_support_number", "sot_obj_unknown_query",
}

ORPHAN_TAG = f"ORPHAN({DATE})"


def _split_flow_blocks(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Return (preamble, [(flow_id, block_text_with_key], ...]) preserving order.

    Preamble = everything before the first ``^  <id>:`` flow key (comments,
    blank lines). Each block runs until the next such line or a top-level
    (column-0) non-blank line. The preamble is returned separately so the
    caller can re-emit it verbatim (preserving comment headers under ``flows:``).
    """
    lines = text.splitlines(keepends=True)
    blocks: list[tuple[str, str]] = []
    cur_id: str | None = None
    cur: list[str] = []
    preamble: list[str] = []
    in_preamble = True
    for line in lines:
        m = re.match(r"^  ([a-z_][a-z0-9_]*):\s*$", line)
        if m:
            if cur_id is not None:
                blocks.append((cur_id, "".join(cur)))
            elif in_preamble:
                preamble.extend(cur)
            cur_id = m.group(1)
            cur = [line]
            in_preamble = False
        elif cur_id is not None and re.match(r"^\S", line):
            blocks.append((cur_id, "".join(cur)))
            cur_id = None
            cur = [line]
        elif cur_id is not None:
            cur.append(line)
        elif in_preamble:
            preamble.append(line)
        else:
            cur.append(line)
    if cur_id is not None:
        blocks.append((cur_id, "".join(cur)))
    return "".join(preamble), blocks


def _process_yaml(path: Path) -> tuple[int, int]:
    """Delete DELETABLE flows + tag AMBIGUOUS. Returns (deleted, tagged)."""
    raw = path.read_text(encoding="utf-8")
    # Isolate the ``flows:`` section so we only rewrite flow blocks, not other keys.
    m = re.search(r"^flows:\n", raw, re.MULTILINE)
    if not m:
        return 0, 0
    head = raw[: m.end()]
    body = raw[m.end():]
    # Split body into flow blocks + any trailing top-level keys.
    # Find where the first top-level (col-0) key after flows: starts.
    tail_match = re.search(r"\n\S", body)
    flows_body = body[: tail_match.start()] if tail_match else body
    tail = body[tail_match.start():] if tail_match else ""

    blocks_text = flows_body
    tail = body[tail_match.start():] if tail_match else ""

    preamble, blocks = _split_flow_blocks(blocks_text)
    deleted = 0
    tagged = 0
    out_parts: list[str] = []
    if preamble:
        out_parts.append(preamble)
    for fid, block in blocks:
        if fid in DELETABLE:
            deleted += 1
            continue
        if fid in AMBIGUOUS and ORPHAN_TAG not in block:
            out_parts.append(f"# {ORPHAN_TAG}: candidate orphan, kept due to FORCE_FLOW_ALIASES ref. Next sweep: re-check after alias cleanup.\n")
            tagged += 1
        out_parts.append(block)
    new_body = "".join(out_parts) + tail
    new_raw = head + new_body
    if new_raw != raw:
        path.write_text(new_raw, encoding="utf-8")
    return deleted, tagged


def _process_manifest() -> int:
    if not MANIFEST.is_file():
        return 0
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    removed = 0
    # Manifest is keyed by reply_id; flows map to reply_ids via the flow's utter.
    # Remove any entry whose key matches a deletable flow name.
    for fid in list(data.keys()):
        if fid in DELETABLE:
            del data[fid]
            removed += 1
    if removed:
        MANIFEST.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return removed


def main() -> None:
    total_deleted = 0
    total_tagged = 0
    print(f"Processing flow YAMLs under {FLOWS_DIR} ...")
    for yml in sorted(FLOWS_DIR.rglob("*.yml")):
        d, t = _process_yaml(yml)
        if d or t:
            print(f"  {yml.relative_to(ROOT)}: deleted={d} tagged={t}")
            total_deleted += d
            total_tagged += t
    print(f"\nProcessing reply_manifest.json ...")
    rm = _process_manifest()
    print(f"  removed {rm} manifest entries")
    print()
    print(f"TOTAL: flows_deleted={total_deleted} flows_tagged={total_tagged} manifest_entries_removed={rm}")
    print(f"DELETABLE set size={len(DELETABLE)} AMBIGUOUS set size={len(AMBIGUOUS)}")


if __name__ == "__main__":
    main()
