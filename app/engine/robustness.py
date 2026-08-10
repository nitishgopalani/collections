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

# Consecutive med/high anger|frustration turn counter (persisted in slots).
FRUSTRATION_COUNT_KEY = "_frustration_turns"
FRUSTRATION_ESCALATION_DISPOSITION = "ESCALATED_FRUSTRATION"
FRUSTRATION_EMOTIONS: frozenset[str] = frozenset({"anger", "frustration"})

# HARDEN-1 F3(b): persisted flag set at the end of a turn whose final gated reply
# was empty/failed. The NEXT turn's track_slot_reask reads it and skips the
# repair-counter increment — the re-ask is the agent's fault (it failed to
# speak), not the borrower's fault (they did answer). Overwritten every turn,
# so it only affects the immediately following turn. Extends routing_miss.
AGENT_FAULT_KEY = "_agent_fault_prev_turn"

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

    # Phase 4: count spoken reply_ids for attempt-indexed template selection.
    # Skip once the call is closing — hangup already clears counters.
    call_closing = bool(
        slots.get("end_call")
        or slots.get("sot_call_closed")
        or any(
            str(k).endswith("call_closed") and slots.get(k) for k in slots
        )
    )
    if reply_id and not call_closing:
        from app.engine.nlg import REPLY_COUNTS_KEY

        counts = dict(slots.get(REPLY_COUNTS_KEY) or {})
        try:
            counts[reply_id] = int(counts.get(reply_id, 0)) + 1
        except (TypeError, ValueError):
            counts[reply_id] = 1
        slots[REPLY_COUNTS_KEY] = counts

    updated.slots = slots
    return updated


def track_slot_reask(
    state: ConversationState,
    *,
    question_slot: str | None,
    had_inbound: bool,
    max_retries: int,
    routing_miss: bool = False,
    agent_fault: bool = False,
) -> tuple[ConversationState, bool]:
    """Track consecutive re-asks of the same collect slot (repair layer F1).

    A "re-ask" is when the executor wants to ask the *same* slot it asked on the
    previous turn, even though the caller replied in between — i.e. the answer did
    not advance the flow. Once the same slot has already been re-asked
    ``max_retries`` times we return ``escalate=True`` instead of counting a further
    re-ask, so the caller is handed off gracefully rather than looping forever.

    When ``routing_miss`` is True the borrower spoke but the engine could not
    confidently route (e.g. Layer-3 weak-jump suppression). Skip increment and
    escalate checks so the miss does not burn borrower retries; the slot-changed
    reset branch still runs.

    When ``agent_fault`` is True the PREVIOUS turn's final gated reply was
    empty/failed (HARDEN-1 F3(b)) — the re-ask on this turn is the agent's fault,
    not the borrower's, so we skip the increment and escalate checks the same way
    routing_miss does. The flag is read from ``slots[AGENT_FAULT_KEY]`` (set by
    :func:`record_agent_fault` at the end of the prior turn) and cleared once
    consumed so a later healthy turn does not inherit it.

    Counters live in ``slots[REPAIR_COUNTS_KEY]``. The previous turn's awaited slot
    is read from ``slots['last_question_slot']`` (written by
    :func:`record_outbound_context` on the prior turn, before it is overwritten
    for this turn).
    """
    updated = state.model_copy(deep=True)
    slots = dict(updated.slots)
    counts: dict[str, int] = dict(slots.get(REPAIR_COUNTS_KEY) or {})
    prev_slot = slots.get("last_question_slot")
    # HARDEN-1 F3(b): consume the agent-fault flag from the prior turn. A failed
    # reply is not a borrower miss, so neither the increment nor the escalate
    # check should fire for this re-ask.
    agent_fault = bool(agent_fault or slots.get(AGENT_FAULT_KEY))
    slots.pop(AGENT_FAULT_KEY, None)

    escalate = False
    if question_slot and had_inbound and question_slot == prev_slot:
        if not routing_miss and not agent_fault:
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


def record_agent_fault(
    state: ConversationState,
    *,
    reply_text: str,
) -> ConversationState:
    """Persist whether this turn's final gated reply was empty/failed (F3(b)).

    Called after the gate. When the reply is empty the NEXT turn's
    :func:`track_slot_reask` must skip the repair-counter increment — the re-ask
    is the agent's fault, not the borrower's. The flag is overwritten every turn
    so it only affects the immediately following turn (and is cleared by
    ``track_slot_reask`` once consumed).
    """
    updated = state.model_copy(deep=True)
    slots = dict(updated.slots)
    fault = not (reply_text or "").strip()
    if fault:
        slots[AGENT_FAULT_KEY] = True
    else:
        slots.pop(AGENT_FAULT_KEY, None)
    updated.slots = slots
    return updated


# W2-4 enforce-coupled repair rule. See W2_SPRINT_SPEC §W2-2 + §W2-4:
# "Repair counter increments ONLY on failed confirms — replace
# agent_fault/routing_miss special-cases with the one rule, their log
# fields become reasons."
#
# A "failed confirm" = the prior turn's Commitment Gate downgraded to
# ``confirm_<slot>`` (a confirm-ask was issued) AND the borrower's
# response this turn did NOT explicitly confirm (evidence < 3 at the
# confirm slot). That is the ONE condition that increments the repair
# counter. ``routing_miss`` and ``agent_fault`` no longer skip the
# increment — they are logged as ``repair_reason`` in guards.
PENDING_CONFIRM_KEY = "_pending_confirm"
REPAIR_REASON_KEY = "_repair_reason"


def set_pending_confirm(
    state: ConversationState,
    *,
    slot: str,
    fragment_id: str | None,
) -> ConversationState:
    """Record that this turn issued a confirm-ask (gate downgrade).

    Called in the commit band when the gate verdict is ``downgrade`` and
    enforce is on. The NEXT turn's ``track_slot_reask_gated`` reads this
    and decides whether the confirm succeeded (evidence >= 3) or failed
    (evidence < 3 → increment the repair counter).
    """
    updated = state.model_copy(deep=True)
    slots = dict(updated.slots)
    slots[PENDING_CONFIRM_KEY] = {"slot": slot, "fragment_id": fragment_id}
    updated.slots = slots
    return updated


def track_slot_reask_gated(
    state: ConversationState,
    *,
    question_slot: str | None,
    had_inbound: bool,
    max_retries: int,
    evidence_score: int,
    routing_miss: bool = False,
    agent_fault: bool = False,
    prior_pending_confirm: dict | None = None,
) -> tuple[ConversationState, bool, str | None]:
    """W2-4 enforce-coupled repair counter.

    Increments the per-slot repair counter ONLY on failed confirms: the
    prior turn issued a confirm-ask (``prior_pending_confirm`` captured
    BEFORE the gate ran this turn) AND this turn's evidence score < 3
    (the borrower did not explicitly confirm). ``routing_miss`` and
    ``agent_fault`` are logged as reasons but do NOT skip the increment.

    Does NOT pop ``_pending_confirm`` from state — the gate manages that
    (sets it on downgrade, clears it on execute/hold). The caller captures
    the prior turn's ``_pending_confirm`` before the gate runs and passes
    it here, so the check reflects the PRIOR confirm-ask, not the one the
    gate may have just set this turn.

    Returns ``(updated_state, escalate, reason)``.
    """
    updated = state.model_copy(deep=True)
    slots = dict(updated.slots)
    counts: dict[str, int] = dict(slots.get(REPAIR_COUNTS_KEY) or {})
    agent_fault = bool(agent_fault or slots.get(AGENT_FAULT_KEY))
    slots.pop(AGENT_FAULT_KEY, None)

    escalate = False
    reason: str | None = None

    pending = prior_pending_confirm
    failed_confirm_slot: str | None = None
    if isinstance(pending, dict) and pending.get("slot"):
        pslot = str(pending["slot"])
        if had_inbound and evidence_score < 3:
            failed_confirm_slot = pslot
            reason = "failed_confirm"

    inc_slot = failed_confirm_slot
    if failed_confirm_slot and had_inbound:
        prior = int(counts.get(inc_slot, 0))
        if prior >= max_retries:
            escalate = True
            reason = "failed_confirm_escalate"
        else:
            counts[inc_slot] = prior + 1
            reason = "failed_confirm"
        if routing_miss:
            reason = (reason + "+routing_miss") if reason else "routing_miss"
        if agent_fault:
            reason = (reason + "+agent_fault") if reason else "agent_fault"
    elif question_slot and question_slot != slots.get("last_question_slot"):
        prev_slot = slots.get("last_question_slot")
        if isinstance(prev_slot, str):
            counts.pop(prev_slot, None)
        counts.setdefault(question_slot, 0)

    slots[REPAIR_COUNTS_KEY] = counts
    if reason:
        slots[REPAIR_REASON_KEY] = reason
    else:
        slots.pop(REPAIR_REASON_KEY, None)
    updated.slots = slots
    return updated, escalate, reason


def track_frustration(
    state: ConversationState,
    *,
    emotion: str | None,
    intensity: str | None,
    threshold: int,
) -> tuple[ConversationState, bool]:
    """Count consecutive med/high anger|frustration turns; escalate at ``threshold``.

    The emotion engine already classifies frustration per turn but today only nudges
    tone. Sustained frustration is a signal the borrower is stuck/upset (the last call
    looped until the borrower threatened a complaint), so once ``threshold`` consecutive
    med-or-high anger/frustration turns are seen we hand off gracefully — the same
    callback path as the repair layer. A calmer turn resets the counter. ``threshold``
    of 0 disables the guard. Returns (state, escalate).
    """
    if threshold <= 0:
        return state, False
    updated = state.model_copy(deep=True)
    slots = dict(updated.slots)
    count = int(slots.get(FRUSTRATION_COUNT_KEY) or 0)
    hot = (emotion in FRUSTRATION_EMOTIONS) and (intensity in {"med", "high"})
    if hot:
        count += 1
    else:
        count = 0
    escalate = count >= threshold
    slots[FRUSTRATION_COUNT_KEY] = 0 if escalate else count
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
    from app.engine.nlg import clear_reply_counts

    clear_reply_counts(slots)
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
