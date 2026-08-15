from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class FlowBranch(BaseModel):
    if_: str | None = Field(default=None, alias="if")
    then: str | None = None
    else_: str | None = Field(default=None, alias="else")

    model_config = {"populate_by_name": True}


class FlowStep(BaseModel):
    """A single step in a flow. Only one action type should be set per step."""

    id: str | None = None
    collect: str | None = None
    action: str | None = None
    utter: str | None = None
    decide: list[FlowBranch] | None = None
    next: str | list[FlowBranch] | None = None
    end: bool | None = None
    # After the highest attempt-tagged utter has already played, jump here instead
    # of replaying (e.g. branch referral / hangup). Ignored when unset.
    escalate_to: str | None = None

    @field_validator("next", mode="before")
    @classmethod
    def normalize_next(cls, value: Any) -> str | list[FlowBranch] | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return [FlowBranch.model_validate(item) for item in value]
        raise ValueError("next must be a string or list of branches")


class Flow(BaseModel):
    description: str
    priority: str
    steps: list[FlowStep]
    # W2-4 E1: Commitment Gate cost class for start_flow of this flow.
    # script_reask (default) = answer / on-rail / informational objection.
    # escalate = genuine handoff / repair-escalate / dispute-raise.
    # Untagged flows default to script_reask — the gate does NOT infer
    # from name substrings (obj_ / dispute / handoff).
    gate_class: str | None = None


class ResponseTemplate(BaseModel):
    text: str
    language: str | None = None  # hi | hinglish | en — DECISION NEEDED: v1 languages
    tone_register: str | None = None  # standard | de_escalate | reassure | dignity | ...
    # 1-based attempt index for objection escalation. When any variant in a
    # reply_id group sets this, NLG selects deterministically by `_reply_counts`
    # instead of random/rotation pick.
    attempt: int | None = None


PriorityType = Literal[
    "opt_out",
    "vulnerable",
    "identity",
    "dispute",
    "hardship",
    "ptp",
    "refusal",
    "reminder",
]


class FlowSet(BaseModel):
    flows: dict[str, Flow]
    responses: dict[str, list[ResponseTemplate]] = Field(default_factory=dict)
