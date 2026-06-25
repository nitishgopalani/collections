from typing import Any, Literal

from pydantic import BaseModel, Field


class TurnRequest(BaseModel):
    call_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    borrower_id: str = Field(min_length=1)
    channel: Literal["voice", "whatsapp"] = "voice"
    transcript: str = Field(min_length=1)
    locale: str = "hi-IN"
    turn_meta: dict[str, Any] = Field(default_factory=dict)


class TurnResponse(BaseModel):
    reply_text: str
    end_call: bool = False
    transfer_to_human: bool = False
    actions_executed: list[str] = Field(default_factory=list)
    disposition: str | None = None
    state_version: int = 0
    audit_id: str = ""
