"""CP-TEST Layer 3 — scenario × canonical-line matrix."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from app.config import get_settings
from app.engine.retrieval import clear_retrieval_cache
from app.engine.tenant_profile import clear_tenant_profile_cache
from app.flows.loader import reload_flow_set
from tests.fixtures.replay import diff_expect, replay_fixture

ROOT = Path(__file__).resolve().parents[2]
MATRIX_PATH = Path(__file__).resolve().parent / "paisalo_matrix.yml"
DOCS = ROOT / "docs" / "testing"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("SCRIPTED_CATALOG_ROUTING", "true")
    monkeypatch.setenv("CALL_WINDOW_START", "00:00")
    monkeypatch.setenv("CALL_WINDOW_END", "23:59")
    monkeypatch.setenv("COMMITMENT_GATE_ENFORCE", "true")
    clear_tenant_profile_cache()
    get_settings.cache_clear()
    reload_flow_set()
    clear_retrieval_cache()
    yield
    clear_retrieval_cache()
    clear_tenant_profile_cache()
    get_settings.cache_clear()


def _load() -> dict:
    return yaml.safe_load(MATRIX_PATH.read_text(encoding="utf-8"))


def _expect_for(line: dict, scenario: str) -> dict:
    block = line.get("expect") or {}
    merged = dict(block.get("default") or {})
    merged.update(block.get(scenario) or {})
    return merged


@pytest.mark.asyncio
async def test_paisalo_scenario_matrix():
    spec = _load()
    setup_turns = spec.get("setup_turns") or {}
    scenarios = spec.get("scenarios") or []
    lines = spec.get("lines") or []
    cells: list[dict] = []
    fails = 0
    for scen in scenarios:
        sid = str(scen["id"])
        for line in lines:
            lid = str(line["id"])
            mode = str(line.get("setup") or "after_identity")
            turns: list[dict] = []
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
                "id": f"matrix-{sid}-{lid}",
                "tenant_id": "paisalo",
                "scenario": sid,
                "borrower_id": "plo_test_borrower",
                "turns": turns,
            }
            rows = await replay_fixture(fixture)
            actual = rows[-1]
            expect = _expect_for(line, sid)
            diffs = diff_expect(expect, actual)
            ok = not diffs
            if not ok:
                fails += 1
            cells.append(
                {
                    "scenario": sid,
                    "line": lid,
                    "ok": ok,
                    "reply_id": actual.get("reply_id"),
                    "diffs": diffs,
                }
            )

    scen_ids = [str(s["id"]) for s in scenarios]
    line_ids = [str(ln["id"]) for ln in lines]
    by_key = {(c["scenario"], c["line"]): c for c in cells}
    header = "| line | " + " | ".join(scen_ids) + " |"
    sep = "|---| " + " | ".join("---" for _ in scen_ids) + " |"
    body = []
    for lid in line_ids:
        cols = []
        for sid in scen_ids:
            cell = by_key[(sid, lid)]
            mark = "PASS" if cell["ok"] else "FAIL"
            rid = cell.get("reply_id") or "∅"
            cols.append(f"{mark}<br>`{rid}`")
        body.append(f"| `{lid}` | " + " | ".join(cols) + " |")
    total = len(cells)
    passed = total - fails
    md = [
        f"# PaisaLo matrix — {date.today().isoformat()}",
        "",
        f"**{passed}/{total} PASS.** Target: 100% green before any pilot dial.",
        "",
        header,
        sep,
        *body,
        "",
    ]
    if fails:
        md.append("## Failures")
        md.append("")
        for cell in cells:
            if cell["ok"]:
                continue
            md.append(f"- `{cell['scenario']}` / `{cell['line']}` `{cell['reply_id']}`")
            for d in cell["diffs"]:
                md.append(f"  - {d}")
        md.append("")
    DOCS.mkdir(parents=True, exist_ok=True)
    out = DOCS / f"MATRIX_{date.today().isoformat()}.md"
    out.write_text("\n".join(md), encoding="utf-8")
    if fails:
        pytest.fail(f"matrix {fails}/{total} FAIL — see {out.as_posix()}")
