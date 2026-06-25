"""Persona classification schema (Sprint 10 / blueprint Engine 2)."""

from pydantic import BaseModel, Field


class PersonaClassification(BaseModel):
    primary_persona: str
    secondary_persona: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    blend: dict[str, float] = Field(default_factory=dict)
    ability: str | None = None
    willingness: str | None = None
    source: str = "rules"
