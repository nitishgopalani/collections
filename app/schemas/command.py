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
