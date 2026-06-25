"""Recovery probability schema (Sprint 13 / blueprint Engine 6)."""

from pydantic import BaseModel, Field


class RecoveryScore(BaseModel):
    p_cure: float = Field(ge=0.0, le=1.0)
    expected_recovery_pv: float = Field(ge=0.0)
    last_scored: str
    method: str = "heuristic_v1"
    explain: dict[str, float | str] = Field(default_factory=dict)
