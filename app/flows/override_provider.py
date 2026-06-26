"""Brand override pack resolution seam (BP-1.3)."""

from __future__ import annotations

from typing import Protocol

from app.config import get_settings
from app.schemas.overrides import BrandOverridePack


class OverrideProvider(Protocol):
    async def get_pack(
        self,
        *,
        agent_id: str | None,
        pack_id: str | None,
    ) -> BrandOverridePack | None: ...


class NullOverrideProvider:
    """Default — no brand customization."""

    async def get_pack(
        self,
        *,
        agent_id: str | None,
        pack_id: str | None,
    ) -> BrandOverridePack | None:
        _ = agent_id, pack_id
        return None


class FixtureOverrideProvider:
    """In-memory packs keyed by agent_id; never raises on miss."""

    def __init__(self, packs: dict[str, BrandOverridePack]) -> None:
        self._packs = dict(packs)

    async def get_pack(
        self,
        *,
        agent_id: str | None,
        pack_id: str | None,
    ) -> BrandOverridePack | None:
        if agent_id is None:
            return None
        pack = self._packs.get(agent_id)
        if pack is None:
            return None
        if pack_id is not None and pack_id != pack.pack_id:
            return None
        return pack


def create_override_provider() -> OverrideProvider:
    """Null provider by default; fixture packs when override_fixtures_mode is on."""
    settings = get_settings()
    if settings.override_fixtures_mode:
        from app.flows.override_fixtures import FIXTURE_PACKS_BY_AGENT

        return FixtureOverrideProvider(FIXTURE_PACKS_BY_AGENT)
    return NullOverrideProvider()
