"""Brand Console v0 — /admin/v0 (CP-UI0).

  - profile PUT invalid → 422 with field errors
  - compliance-dry-run flags a prohibited line
  - test-turn returns guards for a willing turn
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.engine.fragment_library import clear_fragment_cache
from app.engine.retrieval import clear_retrieval_cache
from app.engine.tenant_profile import clear_tenant_profile_cache
from app.flows.loader import reload_flow_set
from app.main import app, lifespan
from tests.helpers.scripted_clients import ScriptedKB, ScriptedLLM

TENANT = "paisalo"
CALL_DATE = "2026-08-15"


@pytest.fixture
def enable_admin(monkeypatch):
    monkeypatch.setenv("ADMIN_API_ENABLED", "true")
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("SCRIPTED_CATALOG_ROUTING", "true")
    monkeypatch.setenv("CALL_WINDOW_START", "00:00")
    monkeypatch.setenv("CALL_WINDOW_END", "23:59")
    monkeypatch.setenv("TEST_PLO_SCENARIO", "postdue3")
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


@pytest.fixture
async def admin_client(enable_admin):
    async with lifespan(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.mark.asyncio
async def test_admin_disabled_returns_404(monkeypatch):
    monkeypatch.setenv("ADMIN_API_ENABLED", "false")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/admin/v0/tenants")
    assert resp.status_code == 404
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_profile_put_invalid_returns_422_field_errors(admin_client: AsyncClient):
    resp = await admin_client.put(
        f"/admin/v0/tenant/{TENANT}/profile",
        json={
            "patch": {
                "dpdp_third_party_lock": "maybe",
                "ptp_policy": {"max_ptp_days": 999},
            }
        },
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["ok"] is False
    fields = {row["field"] for row in body["errors"]}
    assert "dpdp_third_party_lock" in fields
    assert "ptp_policy.max_ptp_days" in fields


@pytest.mark.asyncio
async def test_compliance_dry_run_flags_prohibited_line(admin_client: AsyncClient):
    resp = await admin_client.post(
        f"/admin/v0/tenant/{TENANT}/compliance-dry-run",
        json={"texts": ["police aayegi", "Namaste, main Anjali bol rahi hoon."]},
    )
    assert resp.status_code == 200
    results = {row["text"]: row for row in resp.json()["results"]}
    banned = results["police aayegi"]
    assert banned["verdict"] == "fail"
    assert "prohibited" in banned["reason"]
    ok = results["Namaste, main Anjali bol rahi hoon."]
    assert ok["verdict"] in {"pass", "allowlisted"}


@pytest.mark.asyncio
async def test_test_turn_returns_guards_for_willing(admin_client: AsyncClient):
    app.state.kb = ScriptedKB([])
    app.state.llm = ScriptedLLM([[], [], []])
    session_id = "ui0-willing"
    opener = await admin_client.post(
        f"/admin/v0/tenant/{TENANT}/test-turn",
        json={"session_id": session_id, "transcript": "", "scenario": "postdue3"},
    )
    assert opener.status_code == 200
    assert opener.json()["reply_text"]

    ident = await admin_client.post(
        f"/admin/v0/tenant/{TENANT}/test-turn",
        json={
            "session_id": session_id,
            "transcript": "हाँ, मैं रमेश बोल रहा हूँ।",
        },
    )
    assert ident.status_code == 200

    willing = await admin_client.post(
        f"/admin/v0/tenant/{TENANT}/test-turn",
        json={
            "session_id": session_id,
            "transcript": "ठीक है कर दूंगा।",
        },
    )
    assert willing.status_code == 200
    body = willing.json()
    assert body["reply_text"]
    guards = body["guards"]
    assert guards["evidence"] is not None
    assert 0 <= int(guards["evidence"]) <= 3
    assert guards["gate_verdict"] in {
        "execute",
        "downgrade",
        "downgrade_to_confirm",
        "hold",
    }
    assert "llm_call_reason" in guards
    assert guards["llm_call_reason"] in {"cue_hit", "cache", "called", "skipped"}
    assert isinstance(guards["fragment_ids"], list)


@pytest.mark.asyncio
async def test_exports_date_kind_returns_rows(admin_client: AsyncClient, tmp_path, monkeypatch):
    monkeypatch.setenv("EXPORTS_DIR", str(tmp_path))
    day = tmp_path / "dispositions_20260815.jsonl"
    day.write_text(
        '{"session_id":"ui4ptp","borrower_id":"b1","tenant":"paisalo",'
        '"scenario":"postdue3","disposition":"PTP_SET","ptp_date":"2026-08-20",'
        '"ptp_amount":4500,"flags":["payment_claimed"],"call_ts":"2026-08-15T10:00:00",'
        '"duration":90}\n',
        encoding="utf-8",
    )
    work = tmp_path / "worklist_20260815.jsonl"
    work.write_text(
        '{"session_id":"ui4dnc","borrower_id":"b2","tenant":"paisalo",'
        '"disposition":"dnc_requested","snippet":"please do not call me again thank you",'
        '"flags":["dnc_requested"]}\n',
        encoding="utf-8",
    )
    disp = await admin_client.get("/admin/v0/exports?date=20260815&kind=dispositions")
    assert disp.status_code == 200
    rows = disp.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["disposition"] == "PTP_SET"
    assert rows[0]["ptp_amount"] == 4500
    empty = await admin_client.get("/admin/v0/exports?date=20260816&kind=callbacks")
    assert empty.status_code == 200
    assert empty.json()["rows"] == []
    wl = await admin_client.get("/admin/v0/exports?date=20260815&kind=worklist")
    assert wl.status_code == 200
    assert "do not call" in wl.json()["rows"][0]["snippet"]


@pytest.mark.asyncio
async def test_fragment_put_blocked_line_cannot_save_active(admin_client: AsyncClient):
    before = await admin_client.get(f"/admin/v0/tenant/{TENANT}/fragments")
    assert before.status_code == 200
    digest = before.json()["yaml_hash"]
    resp = await admin_client.put(
        f"/admin/v0/tenant/{TENANT}/fragment/fact_amount_due",
        json={"yaml_hash": digest, "text": "police aayegi, jaldi pay karo"},
    )
    assert resp.status_code == 422
    assert "blocked" in resp.json()["detail"]
    after = await admin_client.get(f"/admin/v0/tenant/{TENANT}/fragments")
    assert after.json()["yaml_hash"] == digest
    row = next(f for f in after.json()["fragments"] if f["id"] == "fact_amount_due")
    assert "police" not in (row.get("text") or "")
