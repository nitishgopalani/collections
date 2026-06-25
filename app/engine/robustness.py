"""Robustness helpers — repeat/clarify hardening and outbound context (FS-5)."""

from __future__ import annotations

from typing import Any

from app.schemas.state import ConversationState

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
