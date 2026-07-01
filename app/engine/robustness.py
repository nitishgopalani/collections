"""Robustness helpers — repeat/clarify hardening and outbound context (FS-5)."""

from __future__ import annotations

import logging
from typing import Any

from app.schemas.state import ConversationState

logger = logging.getLogger(__name__)

# Per-slot re-ask counters live here inside `slots` so they persist across turns
# without a schema change. Keyed by the awaited slot name → number of re-asks so far.
REPAIR_COUNTS_KEY = "_repair_counts"
REPAIR_ESCALATION_DISPOSITION = "ESCALATED_UNCLEAR"

CRITICAL_CONFIRM_SLOTS: frozenset[str] = frozenset(
    {
        "partial_amount",
        "ptp_date",
        "identity_response",
        "payment_rail",
    }
)

MAX_PROMPT_REPEAT_BEFORE_CONFIRM = 2


def record_outbound_context(
    state: ConversationState,
    *,
    reply_id: str | None,
    question_slot: str | None,
    draft: str,
) -> ConversationState:
    """Persist last prompt context so repeat/clarify can re-utter cleanly."""
    if not reply_id and not question_slot and not draft:
        return state

    updated = state.model_copy(deep=True)
    slots = dict(updated.slots)

    if question_slot:
        slots["last_question_slot"] = question_slot
        from app.engine.nlg import COLLECT_SLOT_REPLY_IDS

        mapped = COLLECT_SLOT_REPLY_IDS.get(question_slot)
        if mapped:
            slots["last_reply_id"] = mapped
    elif reply_id:
        slots["last_reply_id"] = reply_id
        slots.pop("last_question_slot", None)

    updated.slots = slots
    return updated


def track_slot_reask(
    state: ConversationState,
    *,
    question_slot: str | None,
    had_inbound: bool,
    max_retries: int,
) -> tuple[ConversationState, bool]:
    """Track consecutive re-asks of the same collect slot (repair layer F1).

    A "re-ask" is when the executor wants to ask the *same* slot it asked on the
    previous turn, even though the caller replied in between — i.e. the answer did
    not advance the flow. Once the same slot has already been re-asked
    ``max_retries`` times we return ``escalate=True`` instead of counting a further
    re-ask, so the caller is handed off gracefully rather than looping forever.

    Counters live in ``slots[REPAIR_COUNTS_KEY]``. The previous turn's awaited slot
    is read from ``slots['last_question_slot']`` (written by
    :func:`record_outbound_context` on the prior turn, before it is overwritten
    for this turn).
    """
    updated = state.model_copy(deep=True)
    slots = dict(updated.slots)
    counts: dict[str, int] = dict(slots.get(REPAIR_COUNTS_KEY) or {})
    prev_slot = slots.get("last_question_slot")

    escalate = False
    if question_slot and had_inbound and question_slot == prev_slot:
        prior = int(counts.get(question_slot, 0))
        if prior >= max_retries:
            escalate = True
        else:
            counts[question_slot] = prior + 1
    elif question_slot and question_slot != prev_slot:
        # Flow advanced to a different slot: clear the stale counter for the one
        # we just left so a later return to it starts fresh.
        if isinstance(prev_slot, str):
            counts.pop(prev_slot, None)
        counts.setdefault(question_slot, 0)

    slots[REPAIR_COUNTS_KEY] = counts
    updated.slots = slots
    return updated, escalate


def mark_repair_escalation(
    state: ConversationState,
    *,
    question_slot: str | None,
) -> ConversationState:
    """Record a graceful callback hand-off after the retry cap is hit (F1).

    We do not have a live transfer path yet (decision Q1 = callback), so we log a
    structured callback record for manual follow-up, set the disposition, and mark
    the call closed so a late barge-in cannot start a new flow.
    """
    updated = state.model_copy(deep=True)
    slots = dict(updated.slots)
    slots["disposition"] = REPAIR_ESCALATION_DISPOSITION
    slots["end_call"] = True
    # Reuse the existing post-close guard so a barge-in reply can't relaunch a flow.
    slots["sot_call_closed"] = True
    slots[REPAIR_COUNTS_KEY] = {}
    updated.slots = slots

    logger.info(
        "repair_callback_scheduled call_id=%s borrower_id=%s stuck_slot=%s",
        updated.call_id,
        updated.borrower_id,
        question_slot,
    )
    return updated


def prepare_repeat(state: ConversationState) -> dict[str, Any]:
    """Compute repeat/clarify branch flags for repeat_request flow."""
    slots = state.slots
    count = int(slots.get("prompt_repeat_count") or 0) + 1
    last_slot = slots.get("last_question_slot")
    last_reply = slots.get("last_reply_id")
    has_last = bool(last_slot or last_reply)
    critical = bool(
        count >= MAX_PROMPT_REPEAT_BEFORE_CONFIRM
        and last_slot
        and str(last_slot) in CRITICAL_CONFIRM_SLOTS
    )
    return {
        "prompt_repeat_count": count,
        "repeat_has_last_prompt": has_last,
        "critical_confirm_needed": critical,
    }


def arm_repeat_from_last(state: ConversationState) -> str | None:
    """Resolve response id to re-render from last outbound context."""
    slots = state.slots
    repeat_id = slots.get("last_reply_id")
    if repeat_id:
        return str(repeat_id)
    last_slot = slots.get("last_question_slot")
    if last_slot:
        from app.engine.nlg import COLLECT_SLOT_REPLY_IDS

        mapped = COLLECT_SLOT_REPLY_IDS.get(str(last_slot))
        if mapped:
            return mapped
    return None


def critical_confirm_slot_label(state: ConversationState) -> str:
    slot = str(state.slots.get("last_question_slot") or "")
    labels = {
        "partial_amount": "amount",
        "ptp_date": "date",
        "identity_response": "verification detail",
        "payment_rail": "payment option",
    }
    return labels.get(slot, "detail")
