from typing import Any

from pydantic import BaseModel, Field


class Frame(BaseModel):
    flow: str
    step_index: int = 0
    parked: bool = False


class Event(BaseModel):
    ts: str
    kind: str
    data: dict[str, Any] = Field(default_factory=dict)
    rationale: str | None = None


class ConversationState(BaseModel):
    call_id: str
    tenant_id: str
    borrower_id: str
    slots: dict[str, Any] = Field(default_factory=dict)
    flow_stack: list[Frame] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    attempts: int = 0
    version: int = 0


class BorrowerRecord(BaseModel):
    borrower_id: str
    identity: dict[str, Any] = Field(default_factory=dict)
    loan: dict[str, Any] = Field(default_factory=dict)
    payments: list[dict[str, Any]] = Field(default_factory=list)
    ptps: list[dict[str, Any]] = Field(default_factory=list)
    broken_ptps: list[dict[str, Any]] = Field(default_factory=list)
    excuses: list[dict[str, Any]] = Field(default_factory=list)
    emotions: list[dict[str, Any]] = Field(default_factory=list)
    hardships: list[dict[str, Any]] = Field(default_factory=list)
    disputes: list[dict[str, Any]] = Field(default_factory=list)
    trust_current: int = 50
    trust_history: list[dict[str, Any]] = Field(default_factory=list)
    risk_flags: list[dict[str, Any]] = Field(default_factory=list)
    recovery: dict[str, Any] = Field(default_factory=dict)
    comms_prefs: dict[str, Any] = Field(default_factory=dict)
    compliance_flags: dict[str, Any] = Field(default_factory=dict)
    notes: list[dict[str, Any]] = Field(default_factory=list)
