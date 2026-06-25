from typing import Any, Literal

from pydantic import BaseModel

CommandType = Literal[
    "start_flow",
    "set_slot",
    "cancel_flow",
    "clarify",
    "human_handoff",
    "cannot_handle",
]


class Command(BaseModel):
    command: CommandType
    flow: str | None = None
    name: str | None = None
    value: Any | None = None
    reason: str | None = None
