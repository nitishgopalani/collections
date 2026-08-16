"""Build stamp for GET /version (G-A1-02) + stale-process detection (CP-TEST)."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_STARTED_AT = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _git(*args: str) -> str:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return (out.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


_START_SHA = _git("rev-parse", "HEAD")
_START_BRANCH = _git("rev-parse", "--abbrev-ref", "HEAD")
_START_COMMIT_TIME = _git("log", "-1", "--format=%cI")


def _short(sha: str) -> str:
    sha = (sha or "").strip()
    if sha in {"", "dev"}:
        return sha
    return sha[:7]


def build_info() -> dict[str, Any]:
    env_sha = (os.getenv("GIT_SHA") or os.getenv("SOURCE_COMMIT") or "").strip()
    env_branch = (os.getenv("GIT_BRANCH") or "").strip()
    running = env_sha or _START_SHA or "dev"
    repo_head = _git("rev-parse", "HEAD")
    branch = env_branch or _START_BRANCH or "dev"
    build_time = (os.getenv("GIT_BUILD_TIME") or "").strip() or _START_COMMIT_TIME or _STARTED_AT
    running_s = _short(running)
    head_s = _short(repo_head)
    stale = False
    if running in {"", "dev"}:
        stale = True
    elif head_s and running_s and running_s != head_s:
        stale = True
    return {
        "git_sha": running,
        "git_branch": branch,
        "build_time": build_time,
        "repo_head": repo_head or running,
        "process_started_at": _STARTED_AT,
        "stale": stale,
    }
