"""Flow / response catalog — version constant and published reply manifest loader."""

import json
from functools import lru_cache
from pathlib import Path

from app.schemas.manifest import ReplyManifest

MANIFEST_VERSION = "1.0.0"
REPLY_MANIFEST_PATH = Path(__file__).resolve().parent / "reply_manifest.json"
TEST_REPLY_MANIFEST_PATH = (
    Path(__file__).resolve().parent / "test_generic" / "reply_manifest.json"
)


@lru_cache(maxsize=4)
def load_reply_manifest(path: Path = REPLY_MANIFEST_PATH) -> ReplyManifest:
    """Load the committed reply manifest (runtime allowlist for brand overrides).

    When ``COLLECTIONS_INCLUDE_TEST_FLOWS`` is set, merge the test-fixture
    manifest (``app/flows/test_generic/reply_manifest.json``) so ``tg_*``
    replies stay out of the production allowlist.
    """
    with path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    from app.flows.loader import include_test_flows

    if include_test_flows() and TEST_REPLY_MANIFEST_PATH.is_file():
        fixture = json.loads(TEST_REPLY_MANIFEST_PATH.read_text(encoding="utf-8"))
        entries = dict(raw.get("entries") or {})
        entries.update(fixture.get("entries") or {})
        raw = {**raw, "entries": entries}
    return ReplyManifest.model_validate(raw)
