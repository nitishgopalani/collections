from typing import Any, Literal

from pydantic import BaseModel

CommandType = Literal[
    "start_flow",
    "set_slot",
    "cancel_flow",
    "clarify",
    "human_handoff",
    "cannot_handle",
    "respond",
    "compose",  # W2-3: compose lane (<=2 fragment ids + oof_class)
]


class Command(BaseModel):
    command: CommandType
    flow: str | None = None
    name: str | None = None
    value: Any | None = None
    reason: str | None = None
    text: str | None = None
    # W2-3 compose command: ordered list of <=2 fragment ids (validated by
    # fragment_library.validate_compose). The renderer renders + appends the
    # canonical re-ask.
    fragments: list[str] | None = None
    # W2-3 router contract: oof_class (9 values) on every compose/respond
    # turn. Telemetry-only input to nothing this phase; logged in
    # turn_decision guards.
    oof_class: str | None = None
    # W2-4 source tagging (invariant #3): every set_slot carries a source.
    # system = hydrated/KB/system-fact · borrower_claim = transcript-derived
    # assertion · confirmed = gate-passed explicit confirm. Borrower
    # assertions NEVER enter system-fact slots (the gate blocks
    # source=borrower_claim writes on money-state slots in enforce mode).
    # Defaults to "system" for backward compat with existing command_gen
    # output that doesn't set it.
    source: str | None = None
