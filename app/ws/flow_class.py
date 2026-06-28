"""Map executor question slots to Go endpointing flow-class hints (EB-6)."""

from typing import Literal

FlowClassHint = Literal["YesNo", "Default", "SpelledInput"]

# Mid-digit / spelled collection prompts — longer endpointing pauses.
SPELLED_INPUT_SLOTS: frozenset[str] = frozenset(
    {
        "identity_response",
        "partial_amount",
        "dispute_claim",
        "callback_window",
        "negotiation_request",
    }
)

# Short acknowledgement-style prompts.
YES_NO_SLOTS: frozenset[str] = frozenset(
    {
        "third_party_borrower_check",
        "hardship_path",
        "identity_confirmed",
        "payment_intent",
        "payment_ack",
    }
)


def flow_class_for_question_slot(slot: str | None) -> FlowClassHint:
    """Derive the next expected input class for SetFlowClass on the Go side."""
    if not slot:
        return "Default"
    normalized = slot.strip().lower()
    if normalized in SPELLED_INPUT_SLOTS:
        return "SpelledInput"
    if normalized in YES_NO_SLOTS:
        return "YesNo"
    return "Default"
