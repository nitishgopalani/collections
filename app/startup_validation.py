"""Fail-fast checks when the brain runs with live LLM/KB (non-stub) clients."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from app.config import Settings

logger = logging.getLogger(__name__)


class LiveConfigError(RuntimeError):
    """Raised when required live-brain env/credentials are missing or invalid."""


def collect_live_config_errors(settings: Settings) -> list[str]:
    """Return human-readable configuration errors (empty list = OK)."""
    errors: list[str] = []

    if not settings.llm_stub_mode:
        if not settings.gcp_project_id.strip():
            errors.append("GCP_PROJECT_ID is required when LLM_STUB=false")
        creds_path = settings.google_application_credentials.strip()
        if not creds_path:
            errors.append(
                "GOOGLE_APPLICATION_CREDENTIALS is required when LLM_STUB=false "
                "(path to mounted service-account JSON inside the container)"
            )
        else:
            path = Path(creds_path)
            if not path.is_file():
                errors.append(
                    f"GOOGLE_APPLICATION_CREDENTIALS file not found: {creds_path} "
                    "(mount the GCP service-account JSON read-only into the brain container)"
                )
            elif not os.access(path, os.R_OK):
                errors.append(f"GOOGLE_APPLICATION_CREDENTIALS not readable: {creds_path}")
            else:
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    errors.append(
                        f"GOOGLE_APPLICATION_CREDENTIALS is not valid JSON: {creds_path} ({exc})"
                    )
                else:
                    if raw.get("type") != "service_account":
                        errors.append(
                            "GOOGLE_APPLICATION_CREDENTIALS must be a GCP service-account JSON "
                            f"(expected type=service_account, got {raw.get('type')!r})"
                        )

    if not settings.kb_stub_mode:
        if not settings.kb_api_key.strip():
            errors.append("KB_API_KEY is required when KB_STUB=false")

    if not settings.stub_mode and settings.llm_stub_mode and settings.kb_stub_mode:
        errors.append(
            "STUB_MODE=false but LLM_STUB and KB_STUB are still true — "
            "set LLM_STUB=false and KB_STUB=false for a real collections conversation"
        )

    return errors


def validate_live_configuration(settings: Settings) -> None:
    """Raise LiveConfigError when live mode is enabled but configuration is incomplete."""
    errors = collect_live_config_errors(settings)
    if errors:
        raise LiveConfigError(
            "Live brain configuration invalid:\n  - " + "\n  - ".join(errors)
        )


def validate_settings_or_exit(settings: Settings) -> None:
    """Log and exit the process when live configuration is invalid (container fail-fast)."""
    try:
        validate_live_configuration(settings)
    except LiveConfigError as exc:
        logger.error("%s", exc)
        print(str(exc), file=sys.stderr)
        sys.exit(1)
