"""UI-6B-3 — fixture suite + 75-cell matrix runners for the publish gate."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import yaml

from tests.fixtures.replay import (
    FIXTURES_DIR,
    diff_expect,
    load_fixture,
    replay_fixture,
)

MATRIX_PATH = Path(__file__).resolve().parents[2] / "tests" / "matrix" / "paisalo_matrix.yml"


def _expect_for(line: dict[str, Any], scenario: str) -> dict[str, Any]:
    block = line.get("expect") or {}
    merged = dict(block.get("default") or {})
    merged.update(block.get(scenario) or {})
    return merged


@contextmanager
def _matrix_env() -> Iterator[None]:
    """Match tests/matrix/test_paisalo_matrix.py autouse env (goldens assume this)."""
    keys = {
        "TEST_MODE": "true",
        "SCRIPTED_CATALOG_ROUTING": "true",
        "CALL_WINDOW_START": "00:00",
        "CALL_WINDOW_END": "23:59",
        "COMMITMENT_GATE_ENFORCE": "true",
    }
    prior = {k: os.environ.get(k) for k in keys}
    os.environ.update(keys)
    from app.config import get_settings
    from app.engine.fragment_library import clear_fragment_cache
    from app.engine.retrieval import clear_retrieval_cache
    from app.engine.tenant_profile import clear_tenant_profile_cache
    from app.flows.loader import reload_flow_set

    clear_tenant_profile_cache()
    clear_fragment_cache()
    get_settings.cache_clear()
    reload_flow_set()
    clear_retrieval_cache()
    try:
        yield
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        clear_tenant_profile_cache()
        clear_fragment_cache()
        get_settings.cache_clear()
        reload_flow_set()
        clear_retrieval_cache()


async def run_fixture_suite() -> dict[str, Any]:
    paths = sorted(FIXTURES_DIR.glob("*.json"))
    with _matrix_env():
        return await _run_fixture_suite(paths)


async def _run_fixture_suite(paths: list[Path]) -> dict[str, Any]:
    failed: list[dict[str, Any]] = []
    for path in paths:
        rows = await replay_fixture(load_fixture(path))
        diffs: list[str] = []
        for row in rows:
            diffs.extend(str(x) for x in (row.get("diffs") or []))
        if diffs:
            failed.append(
                {
                    "id": path.stem,
                    "cell": path.stem,
                    "diffs": diffs[:12],
                }
            )
    total = len(paths)
    return {
        "ok": not failed,
        "total": total,
        "passed": total - len(failed),
        "failed": failed,
    }


async def run_matrix_suite() -> dict[str, Any]:
    with _matrix_env():
        return await _run_matrix_suite()


async def _run_matrix_suite() -> dict[str, Any]:
    spec = yaml.safe_load(MATRIX_PATH.read_text(encoding="utf-8")) or {}
    setup_turns = spec.get("setup_turns") or {}
    scenarios = spec.get("scenarios") or []
    lines = spec.get("lines") or []
    failed: list[dict[str, Any]] = []
    total = 0
    for scen in scenarios:
        sid = str(scen["id"])
        for line in lines:
            total += 1
            lid = str(line["id"])
            mode = str(line.get("setup") or "after_identity")
            turns: list[dict[str, Any]] = []
            if mode == "none":
                turns = [{"transcript": line.get("transcript") or ""}]
            elif mode == "opener":
                turns = [
                    {"transcript": setup_turns["opener"]["transcript"]},
                    {"transcript": line.get("transcript") or ""},
                ]
            else:
                for step in scen.get("setup") or ["opener", "identity_yes"]:
                    turns.append({"transcript": setup_turns[step]["transcript"]})
                turns.append({"transcript": line.get("transcript") or ""})
            fixture = {
                "id": f"publish-matrix-{sid}-{lid}",
                "tenant_id": "paisalo",
                "scenario": sid,
                "borrower_id": "plo_test_borrower",
                "turns": turns,
            }
            rows = await replay_fixture(fixture)
            actual = rows[-1]
            expect = _expect_for(line, sid)
            diffs = diff_expect(expect, actual)
            if diffs:
                failed.append(
                    {
                        "id": f"{sid}/{lid}",
                        "cell": f"{sid}/{lid}",
                        "scenario": sid,
                        "line": lid,
                        "diffs": diffs,
                    }
                )
    return {
        "ok": not failed,
        "total": total,
        "passed": total - len(failed),
        "failed": failed,
    }
