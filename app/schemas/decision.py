"""Decision overlay schema (Sprint 12 / blueprint Engine 8 full)."""

from pydantic import BaseModel, Field


class DecisionSignals(BaseModel):
    trust: int = 50
    bucket: str = "standard"
    ability: str = "medium"
    willingness: str = "medium"
    primary_persona: str | None = None
    emotion: str = "neutral"
    emotion_intensity: str = "med"
    risk_flags: list[str] = Field(default_factory=list)
    p_cure: float = 0.5
    expected_recovery_pv: float = 0.0


class DecisionCandidate(BaseModel):
    action_id: str
    category: str
    compliance_blocked: bool = False
    human_owned: bool = False
    recovery_value: float = 0.0
    contact_cost: float = 0.0
    experience_cost: float = 0.0


class DecisionOverlayResult(BaseModel):
    quadrant: str
    selected_action: str | None = None
    ranked_actions: list[str] = Field(default_factory=list)
    human_recommendations: list[str] = Field(default_factory=list)
    ptp_max_days: int = 14
    pressure_allowed: bool = True
    decision_strategy: str = "standard"
    objective_score: float = 0.0
    source: str = "rules"
