"""Brand override pack schema — multi-variant append/replace per reply_id."""

from pydantic import BaseModel, Field


class BrandVariant(BaseModel):
    language: str | None = None
    tone_register: str | None = None
    text: str


class ReplyOverride(BaseModel):
    reply_id: str
    variants: list[BrandVariant] = Field(default_factory=list)
    replace: bool = False
    enabled: bool = True


class BrandOverridePack(BaseModel):
    agent_id: str
    pack_id: str
    manifest_version: str
    overrides: list[ReplyOverride] = Field(default_factory=list)
