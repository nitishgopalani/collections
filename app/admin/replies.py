"""UI-5 reply catalog — locate / validate / write flow utterances + fragments."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.admin.audit import file_hash
from app.admin.yaml_io import dump_raw, fragments_path, load_raw
from app.engine.fragment_library import get_fragment
from app.engine.gender import missing_gender_token, tokens_used
from app.engine.nlg import CLARIFY_REASK_REPLY_IDS, COLLECT_SLOT_REPLY_IDS
from app.flows.loader import FLOWS_DIR, get_flow_set, reload_flow_set

SYSTEM_REPLY_IDS = frozenset(
    {
        "repair_escalation",
        "compose",
        "respond",
        "ptp_reminder",
        "repeat_call_greeting",
    }
)

_REASK_IDS = frozenset(COLLECT_SLOT_REPLY_IDS.values()) | frozenset(
    CLARIFY_REASK_REPLY_IDS.values()
)

_KNOWN_SLOTS = frozenset(
    {
        "customer_name",
        "persona_name",
        "repay_amount",
        "loan_amount",
        "due_date",
        "disbursal_date",
        "last_date_paid",
        "days_past_due",
        "days_past_due_words",
        "committed_date",
        "offered_amount",
        "remaining_after",
        "amount_paid",
        "last_payment_amount",
        "total_outstanding",
        "emi_remaining",
        "emi_amount",
        "branch",
        "product",
        "helpline",
        "ptp_date",
        "ptp_amount",
        "voice_id",
        "G",
    }
)


def _flow_files() -> list[Path]:
    files = list(FLOWS_DIR.glob("*.yml")) + list(FLOWS_DIR.glob("*/*.yml"))
    return sorted(files)


def find_flow_file(reply_id: str) -> Path | None:
    needle = re.compile(rf"(?m)^  {re.escape(reply_id)}:\s*$")
    for path in _flow_files():
        text = path.read_text(encoding="utf-8")
        if needle.search(text):
            return path
    return None


def _source_kind(reply_id: str, *, is_fragment: bool) -> str:
    if is_fragment:
        return "fragment"
    if reply_id in SYSTEM_REPLY_IDS:
        return "system"
    if reply_id in _REASK_IDS:
        return "reask_template"
    return "flow_utter"


def lookup_reply(tenant_id: str, reply_id: str) -> dict[str, Any] | None:
    """Return the catalog row for a reply_id, or None if unknown."""
    if reply_id in SYSTEM_REPLY_IDS:
        reason = {
            "repair_escalation": "system escalation_reply (config), not a YAML utter",
            "compose": "computed compose-lane join, not a single template",
            "respond": "Tier-3 / unknown_info line, not a flow utter",
            "ptp_reminder": "computed call-history reminder",
            "repeat_call_greeting": "computed repeat-call greeting",
        }.get(reply_id, "computed/system line")
        return {
            "reply_id": reply_id,
            "source_kind": "system",
            "file": "",
            "node": reply_id,
            "text": "",
            "variants": [],
            "tokens_used": [],
            "attempt": None,
            "editable": False,
            "lock_reason": reason,
            "yaml_hash": "",
        }

    frag = get_fragment(tenant_id, reply_id)
    if frag is not None:
        text = str(frag.get("text") or "")
        variants = frag.get("variants") or {}
        variant_rows = (
            [{"id": k, "text": v} for k, v in variants.items()]
            if isinstance(variants, dict)
            else []
        )
        path = fragments_path(tenant_id)
        return {
            "reply_id": reply_id,
            "source_kind": "fragment",
            "file": path.name,
            "node": reply_id,
            "text": text,
            "variants": variant_rows,
            "tokens_used": tokens_used(text),
            "attempt": None,
            "editable": True,
            "lock_reason": "",
            "yaml_hash": file_hash(path) if path.exists() else "",
        }

    flows = get_flow_set()
    variants = flows.responses.get(reply_id)
    if not variants:
        return None
    path = find_flow_file(reply_id)
    rows = []
    for v in variants:
        rows.append(
            {
                "text": v.text,
                "language": v.language,
                "attempt": v.attempt,
                "tone_register": v.tone_register,
            }
        )
    primary = variants[0]
    return {
        "reply_id": reply_id,
        "source_kind": _source_kind(reply_id, is_fragment=False),
        "file": str(path.relative_to(FLOWS_DIR.parent.parent)) if path else "",
        "node": reply_id,
        "text": primary.text,
        "variants": rows,
        "tokens_used": tokens_used(primary.text),
        "attempt": primary.attempt,
        "editable": True,
        "lock_reason": "",
        "yaml_hash": file_hash(path) if path else "",
    }


def validate_reply_text(text: str, *, extra_texts: list[str] | None = None) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    texts = [text] + list(extra_texts or [])
    for line in texts:
        for tok in tokens_used(line):
            if tok != "G" and tok not in _KNOWN_SLOTS:
                errors.append({"field": "text", "error": f"unknown slot {{{tok}}}"})
        if missing_gender_token(line):
            errors.append(
                {
                    "field": "text",
                    "error": "gendered verb requires {G:feminine|masculine}",
                }
            )
    return errors


def _replace_quoted_text(raw: str, old: str, new: str) -> str | None:
    for quote in ('"', "'"):
        needle = f"{quote}{old}{quote}"
        if needle in raw:
            escaped = new.replace("\\", "\\\\").replace(quote, f"\\{quote}")
            return raw.replace(needle, f"{quote}{escaped}{quote}", 1)
    return None


def write_flow_reply(
    reply_id: str,
    *,
    text: str,
    attempt: int | None = None,
) -> Path:
    path = find_flow_file(reply_id)
    if path is None:
        raise FileNotFoundError(reply_id)
    flows = get_flow_set()
    variants = flows.responses.get(reply_id) or []
    target = None
    if attempt is not None:
        for v in variants:
            if v.attempt == attempt:
                target = v
                break
    if target is None:
        target = variants[0] if variants else None
    if target is None:
        raise KeyError(reply_id)
    raw = path.read_text(encoding="utf-8")
    updated = _replace_quoted_text(raw, target.text, text)
    if updated is None:
        raise ValueError("could not locate template text in YAML")
    path.write_text(updated, encoding="utf-8")
    reload_flow_set()
    return path


def write_fragment_reply(tenant_id: str, fid: str, text: str) -> Path:
    path = fragments_path(tenant_id)
    raw = load_raw(path)
    frags = list(raw.get("fragments") or [])
    found = None
    for item in frags:
        if isinstance(item, dict) and item.get("id") == fid:
            found = item
            break
    if found is None:
        raise KeyError(fid)
    found["text"] = text
    raw["fragments"] = frags
    dump_raw(path, raw)
    return path
