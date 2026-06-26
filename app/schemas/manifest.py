"""Published reply_id allowlist — slots and mandatory-lock flags for brand overrides."""

from pydantic import BaseModel, Field


class ReplyEntry(BaseModel):
    """One platform response template key in the published manifest."""

    slots: list[str] = Field(default_factory=list)
    is_mandatory: bool = False


class ReplyManifest(BaseModel):
    """Runtime-trusted catalog of overridable reply_ids."""

    version: str
    entries: dict[str, ReplyEntry]
