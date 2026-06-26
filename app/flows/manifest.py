"""Flow / response catalog — version constant and published reply manifest loader."""

import json
from pathlib import Path

from app.schemas.manifest import ReplyManifest

MANIFEST_VERSION = "1.0.0"
REPLY_MANIFEST_PATH = Path(__file__).resolve().parent / "reply_manifest.json"


def load_reply_manifest(path: Path = REPLY_MANIFEST_PATH) -> ReplyManifest:
    """Load the committed reply manifest (runtime allowlist for brand overrides)."""
    with path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    return ReplyManifest.model_validate(raw)
