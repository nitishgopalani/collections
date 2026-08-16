"""CP-TEST Layer 2 — replay tests/fixtures/console/*.json through the turn pipeline."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import get_settings
from app.engine.fragment_library import clear_fragment_cache
from app.engine.retrieval import clear_retrieval_cache
from app.engine.tenant_profile import clear_tenant_profile_cache
from app.flows.loader import reload_flow_set
from tests.fixtures.replay import FIXTURES_DIR, load_fixture, replay_fixture


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("SCRIPTED_CATALOG_ROUTING", "true")
    monkeypatch.setenv("CALL_WINDOW_START", "00:00")
    monkeypatch.setenv("CALL_WINDOW_END", "23:59")
    monkeypatch.setenv("COMMITMENT_GATE_ENFORCE", "true")
    clear_tenant_profile_cache()
    clear_fragment_cache()
    get_settings.cache_clear()
    reload_flow_set()
    clear_retrieval_cache()
    yield
    clear_retrieval_cache()
    clear_fragment_cache()
    clear_tenant_profile_cache()
    get_settings.cache_clear()


def _fixtures() -> list[Path]:
    return sorted(FIXTURES_DIR.glob("*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
async def test_console_fixture(path: Path):
    fixture = load_fixture(path)
    rows = await replay_fixture(fixture)
    failed = []
    for row in rows:
        if row["diffs"]:
            block = [f"  turn {row['turn']} transcript={row['transcript']!r}"]
            for line in row["diffs"]:
                block.append(f"    {line}")
            failed.append("\n".join(block))
    if failed:
        pytest.fail(f"{path.name}  {len(failed)} turn(s) drifted\n" + "\n".join(failed))
    assert rows, f"{path.name} produced zero turns"
