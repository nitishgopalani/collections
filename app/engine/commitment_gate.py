"""W2-2 Commitment Gate — pure function over (candidate, evidence, cost_table).

SHADOW MODE this phase (default): the gate computes and logs its verdict
(``gate_verdict``, ``would_downgrade``, ``confirm_fragment_id``, ``gate_reason``,
``gate_cost_class``) but does NOT alter behaviour. The existing propose →
tracker_apply → executor path runs unchanged. The ENFORCE flag
(``COMMITMENT_GATE_ENFORCE``, default false) flips the gate to block the
commit path and replace candidate commands with a confirm-ask fragment; that
flip is a W2-2 follow-up after the shadow observation week.

Rule (per W2_SPRINT_SPEC.md §W2-2):
  - cost 0 (script / re-ask)            → execute (always)
  - cost 1 (speak-fact / neutral-slot)  → execute if evidence >= 1
  - cost 2 (escalate / end_call)        → execute if evidence >= 2
  - cost 3 (money-state / PII)         → execute if evidence >= 3
  - evidence == 0 and cost > 0          → hold (non-addressed; confirm is pointless)
  - PII slot without identity_current    → hold (disclosure locked)

The gate consumes ONLY the deterministic evidence score (0-3) from W2-1 —
never the LLM ``confidence`` (invariant #6). It is a pure function: no state
mutation, no I/O. The caller is responsible for logging the verdict and
(in ENFORCE) blocking the commit.
"""

from __future__ import annotations

from typing import Any

from app.schemas.command import Command

# Default cost table (per spec). Tenant YAML overrides per-tenant.
# DEBT-041: identity_confirm (cost 2) is EXEMPT from the identity_current
# precondition — it is the turn that ESTABLISHES identity_current, so gating
# it on identity_current would be a chicken-egg. pii (cost 3) is narrowed to
# personal-data slots only (customer_name/phone/address/dob) and IS keyed on
# identity_current.
DEFAULT_COST_TABLE: dict[str, int] = {
    "script_reask": 0,
    "speak_fact": 1,
    "neutral_slot": 1,
    "escalate": 2,
    "end_call": 2,
    "identity_confirm": 2,
    "money_state": 3,
    "pii": 3,
}

# Money-state slot families (per spec): committed_date, offered_amount,
# willing-commit, already_paid claim. These are matched by substring so
# tenant-prefixed names (plo_*, sot_*) hit.
_MONEY_STATE_MARKERS = (
    "committed_date",
    "offer_amount",
    "offered_amount",
    "payment_intent",  # willing-commit slot (PLO + SOT)
    "timeline",  # plo_timeline (commit-timing)
    "afterdue_decision",
    "ondue_decision",
    "already_paid",
    "partial_amount",
    "ptp_date",
)

# DEBT-041: identity-confirm slots. The turn that confirms identity is the
# turn that SETS identity_current — it must not be keyed on identity_current.
# Matched by substring so plo_identity_response / sot_identity_response hit.
_IDENTITY_CONFIRM_MARKERS = (
    "identity_response",
    "identity_verified",
    "identity_confirm",
)

# DEBT-041: PII is NARROWED to personal-data slots only. Identity-confirmation
# slots are NOT pii (they are identity_confirm, cost 2, exempt from the
# identity_current precondition). Do NOT add "identity" here — that would
# re-introduce the chicken-egg.
_PII_MARKERS = (
    "customer_name",
    "phone",
    "aadhaar",
    "pan",
    "email",
    "address",
    "dob",
    "date_of_birth",
)


def _slot_cost_class(slot_name: str, slot_cost_class: dict[str, str]) -> str:
    """Classify a slot name into a cost class via tenant map + substring heuristics."""
    if not slot_name:
        return "neutral_slot"
    # Tenant-explicit map wins.
    mapped = slot_cost_class.get(slot_name)
    if mapped:
        return mapped
    low = slot_name.lower()
    # DEBT-041: identity_confirm is checked BEFORE pii so identity_response
    # slots are not mis-classified.
    if any(m in low for m in _IDENTITY_CONFIRM_MARKERS):
        return "identity_confirm"
    if any(m in low for m in _PII_MARKERS):
        return "pii"
    if any(m in low for m in _MONEY_STATE_MARKERS):
        return "money_state"
    return "neutral_slot"


def _command_cost_class(
    cmd: Command,
    *,
    slot_cost_class: dict[str, str],
    flow_gate_class: dict[str, str] | None = None,
) -> tuple[str, int | None]:
    """Return (cost_class, slot_name_or_none) for a single command.

    ``slot_name_or_none`` is the target slot for set_slot (used by the caller
    to pick a confirm fragment); None for non-slot commands.

    W2-4 source-aware (invariant #3): a set_slot with ``source=system``
    (hydrated/KB system-fact) or ``source=confirmed`` (gate-passed confirm)
    is TRUSTED — it bypasses the cost check (cost 0). A set_slot with
    ``source=borrower_claim`` (transcript-derived assertion) goes through
    the slot's cost class. Untagged (None) defaults to the slot's cost
    class (the gate treats it as a borrower assertion on money-state slots,
    which is the conservative default).
    """
    if cmd.command == "set_slot":
        # W2-4: trusted sources bypass the cost check.
        if cmd.source in ("system", "confirmed"):
            return "script_reask", cmd.name  # cost 0 — trusted
        cls = _slot_cost_class(cmd.name or "", slot_cost_class)
        return cls, cmd.name
    if cmd.command == "start_flow":
        # E1: class comes from flow YAML ``gate_class`` (passed in
        # flow_gate_class), then tenant slot_cost_class[flow_name] as a
        # back-compat override. Untagged = script_reask. NO name-substring
        # heuristic (obj_ / dispute / handoff) — those mis-classed answer
        # flows like plo_obj_which_emi as escalate (live dc4c5808 t3).
        flow = cmd.flow or ""
        mapped = (flow_gate_class or {}).get(flow) or slot_cost_class.get(flow)
        if mapped:
            return mapped, None
        return "script_reask", None
    if cmd.command in ("end_call", "hangup_call", "transfer_call", "human_handoff"):
        return "end_call", None
    if cmd.command == "respond":
        return "speak_fact", None
    if cmd.command == "clarify":
        return "script_reask", None
    return "neutral_slot", None


def flow_gate_class_map(flows: Any) -> dict[str, str]:
    """Build {flow_name: gate_class} from a FlowSet (or anything with .flows)."""
    out: dict[str, str] = {}
    raw = getattr(flows, "flows", None) or {}
    for name, flow in raw.items():
        cls = getattr(flow, "gate_class", None)
        if cls:
            out[str(name)] = str(cls)
    return out


def commitment_gate(
    candidate: list[Command],
    *,
    evidence: dict[str, Any],
    cost_table: dict[str, int] | None,
    slot_cost_class: dict[str, str] | None,
    identity_ok: bool,
    awaited_slot: str | None,
    flow_gate_class: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Pure function: (candidate, evidence, cost_table) → verdict dict.

    Returns:
        {
            "verdict": "execute" | "downgrade" | "hold",
            "reason": str,
            "confirm_fragment_id": str | None,  # fragment id for the confirm-ask
            "would_downgrade": bool,
            "cost_class": str,  # highest-cost class across candidate
            "max_cost": int,
            "evidence": int,  # echo of the input evidence score
        }
    """
    table = dict(DEFAULT_COST_TABLE)
    if cost_table:
        table.update(cost_table)
    slot_map = dict(slot_cost_class or {})
    flow_map = dict(flow_gate_class or {})
    ev_score = int(evidence.get("evidence", 0) or 0)

    max_cost = 0
    max_class = "script_reask"
    confirm_slot: str | None = None
    pii_without_identity = False
    for cmd in candidate:
        cls, slot = _command_cost_class(
            cmd, slot_cost_class=slot_map, flow_gate_class=flow_map,
        )
        cost = table.get(cls, 1)
        if cost > max_cost:
            max_cost = cost
            max_class = cls
            if slot:
                confirm_slot = slot
        if cls == "pii" and not identity_ok:
            pii_without_identity = True

    # PII without identity_current → hold (disclosure locked).
    if pii_without_identity:
        return {
            "verdict": "hold",
            "reason": "pii_without_identity_current",
            "confirm_fragment_id": None,
            "would_downgrade": False,
            "cost_class": "pii",
            "max_cost": max_cost,
            "evidence": ev_score,
        }

    # Non-addressed turn (evidence 0) with any cost > 0 → hold.
    if ev_score == 0 and max_cost > 0:
        return {
            "verdict": "hold",
            "reason": "non_addressed",
            "confirm_fragment_id": None,
            "would_downgrade": False,
            "cost_class": max_class,
            "max_cost": max_cost,
            "evidence": ev_score,
        }

    # Execute if evidence >= cost; else downgrade to confirm.
    if ev_score >= max_cost:
        return {
            "verdict": "execute",
            "reason": f"evidence_{ev_score}_meets_cost_{max_cost}",
            "confirm_fragment_id": None,
            "would_downgrade": False,
            "cost_class": max_class,
            "max_cost": max_cost,
            "evidence": ev_score,
        }

    # Downgrade to confirm. Value-aware fragment: refused → confirm_<slot>_refused
    # when that id exists; otherwise confirm_<slot> (willing-shaped default).
    frag = None
    confirm_value = None
    if confirm_slot:
        for cmd in candidate:
            if cmd.command == "set_slot" and cmd.name == confirm_slot:
                confirm_value = str(cmd.value) if cmd.value is not None else None
                break
        refused = (confirm_value or "").strip().lower() in {
            "refused", "unwilling", "later", "denied", "no",
        }
        frag = f"confirm_{confirm_slot}_refused" if refused else f"confirm_{confirm_slot}"
    return {
        "verdict": "downgrade",
        "reason": f"evidence_{ev_score}_below_cost_{max_cost}",
        "confirm_fragment_id": frag,
        "would_downgrade": True,
        "cost_class": max_class,
        "max_cost": max_cost,
        "evidence": ev_score,
        "confirm_slot": confirm_slot,
        "confirm_value": confirm_value,
    }


def commitment_gate_enforce_enabled() -> bool:
    """ENFORCE flag (default false = SHADOW)."""
    import os

    return os.getenv("COMMITMENT_GATE_ENFORCE", "").lower() in ("1", "true", "yes")
