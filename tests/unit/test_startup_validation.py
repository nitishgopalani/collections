"""Startup validation for live brain (no Vertex/KB network calls)."""

import json
from pathlib import Path

import pytest

from app.config import Settings
from app.startup_validation import LiveConfigError, collect_live_config_errors, validate_live_configuration


def test_stub_mode_passes_without_creds():
    settings = Settings(stub_mode=True, llm_stub=True, kb_stub=True)
    validate_live_configuration(settings)


def test_live_llm_requires_project_and_creds(tmp_path: Path):
    creds = tmp_path / "sa.json"
    creds.write_text(
        json.dumps({"type": "service_account", "project_id": "test-proj"}),
        encoding="utf-8",
    )
    settings = Settings(
        stub_mode=False,
        llm_stub=False,
        kb_stub=True,
        gcp_project_id="test-proj",
        google_application_credentials=str(creds),
    )
    validate_live_configuration(settings)


def test_live_llm_missing_project():
    settings = Settings(
        stub_mode=False,
        llm_stub=False,
        kb_stub=True,
        gcp_project_id="",
        google_application_credentials="/run/secrets/gcp-sa.json",
    )
    errors = collect_live_config_errors(settings)
    assert any("GCP_PROJECT_ID" in e for e in errors)


def test_live_llm_missing_creds_file():
    settings = Settings(
        stub_mode=False,
        llm_stub=False,
        kb_stub=True,
        gcp_project_id="my-project",
        google_application_credentials="/no/such/file.json",
    )
    with pytest.raises(LiveConfigError, match="not found"):
        validate_live_configuration(settings)


def test_live_kb_requires_api_key():
    settings = Settings(stub_mode=False, llm_stub=True, kb_stub=False, kb_api_key="")
    with pytest.raises(LiveConfigError, match="KB_API_KEY"):
        validate_live_configuration(settings)


def test_stub_mode_false_with_llm_kb_still_stub_warns():
    settings = Settings(stub_mode=False, llm_stub=True, kb_stub=True)
    errors = collect_live_config_errors(settings)
    assert any("STUB_MODE=false" in e for e in errors)


def test_simulate_tools_forbidden_under_asterisk():
    settings = Settings(
        stub_mode=True,
        llm_stub=True,
        kb_stub=True,
        tools_mode="simulate",
        carrier="asterisk",
    )
    errors = collect_live_config_errors(settings)
    assert any("TOOLS_MODE=simulate" in e for e in errors)


def test_invalid_service_account_json(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("not-json", encoding="utf-8")
    settings = Settings(
        stub_mode=False,
        llm_stub=False,
        kb_stub=True,
        gcp_project_id="p",
        google_application_credentials=str(bad),
    )
    with pytest.raises(LiveConfigError, match="not valid JSON"):
        validate_live_configuration(settings)
