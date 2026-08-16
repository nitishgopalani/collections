"""Build stamp for GET /version (G-A1-02)."""

from __future__ import annotations

import os


def build_info() -> dict[str, str]:
    sha = (os.getenv("GIT_SHA") or os.getenv("SOURCE_COMMIT") or "").strip()
    branch = (os.getenv("GIT_BRANCH") or "").strip()
    return {
        "git_sha": sha or "dev",
        "git_branch": branch or "dev",
    }
