"""Emotion classification schema (Sprint 11 / blueprint Engine 3)."""

from pydantic import BaseModel, Field


class EmotionClassification(BaseModel):
    emotion: str
    intensity: str = Field(pattern=r"^(low|med|high)$")
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    channel: str = "text"
    source: str = "rules"
