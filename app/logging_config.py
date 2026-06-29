"""Configure application logging for Docker/uvicorn (INFO to stdout)."""

from __future__ import annotations

import logging
import os
import sys


def configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
        force=True,
    )
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).setLevel(level)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
