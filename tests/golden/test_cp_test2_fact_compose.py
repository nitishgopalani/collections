"""CP-TEST2: deterministic fact-compose (stub LLM must not re-ask)."""

from __future__ import annotations

import pytest

from app.engine.fragment_library import clear_fragment_cache, match_fact_compose
from app.engine.tenant_profile import clear_tenant_profile_cache
from tests.fixtures.replay import replay_fixture


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("SCRIPTED_CATALOG_ROUTING", "true")
    monkeypatch.setenv("CALL_WINDOW_START", "00:00")
    monkeypatch.setenv("CALL_WINDOW_END", "23:59")
    monkeypatch.setenv("COMMITMENT_GATE_ENFORCE", "true")
    clear_tenant_profile_cache()
    clear_fragment_cache()
    yield
    clear_fragment_cache()
    clear_tenant_profile_cache()


def test_match_late_fee_prefers_pre_on_predue():
    hit = match_fact_compose("paisalo", "late fee kitni hai", scenario="predue")
    assert hit == (["fact_penalty_pre"], "call_context")


def test_match_late_fee_post_on_postdue1():
    hit = match_fact_compose("paisalo", "late fee kitni hai", scenario="postdue1")
    assert hit == (["fact_penalty_post"], "call_context")


def test_match_office_and_complaint_and_paid():
    assert match_fact_compose("paisalo", "office kahan se?", scenario="ondue") == (
        ["fact_branch"],
        "call_context",
    )
    assert match_fact_compose("paisalo", "yeh company bekar hai", scenario="npa") == (
        ["ack_neutral", "fact_grievance"],
        "complaint",
    )
    assert match_fact_compose("paisalo", "maine pehle hi de diya", scenario="predue") == (
        ["fact_payment_lag"],
        "payment_assertion",
    )


@pytest.mark.asyncio
async def test_predue_late_fee_composes_not_reask():
    rows = await replay_fixture(
        {
            "id": "cp-test2-predue-late",
            "tenant_id": "paisalo",
            "scenario": "predue",
            "turns": [
                {"transcript": ""},
                {"transcript": "haan, main Ramesh bol raha hoon"},
                {
                    "transcript": "late fee kitni hai",
                    "expect": {
                        "reply_id": "compose",
                        "compose_fragments": ["fact_penalty_pre"],
                        "reply_empty": False,
                    },
                },
            ],
        }
    )
    assert not rows[-1]["diffs"], rows[-1]["diffs"]
