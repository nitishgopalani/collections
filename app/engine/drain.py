"""W4-2 graceful drain — stop new sessions, wait for in-flight (3 min cap)."""

from __future__ import annotations

import logging
import time
from typing import Any

from app.ws.tenant_limits import SESSION_REGISTRY

logger = logging.getLogger(__name__)

DEFAULT_CAP_S = 180.0


class DrainController:
    def __init__(self) -> None:
        self.draining = False
        self.started_mono = 0.0
        self.cap_s = DEFAULT_CAP_S

    def reset(self) -> None:
        self.draining = False
        self.started_mono = 0.0
        self.cap_s = DEFAULT_CAP_S

    def begin(self, cap_s: float | None = None) -> None:
        if self.draining:
            return
        self.draining = True
        self.started_mono = time.monotonic()
        if cap_s is not None:
            self.cap_s = float(cap_s)
        logger.info(
            "drain_started cap_s=%s in_flight=%s",
            self.cap_s,
            self.in_flight(),
        )

    def in_flight(self) -> int:
        return SESSION_REGISTRY.total()

    def wait_idle(self, *, poll_s: float = 0.05) -> bool:
        """Block until no in-flight sessions or cap. True if idle."""
        deadline = time.monotonic() + self.cap_s
        while self.in_flight() > 0 and time.monotonic() < deadline:
            logger.info("drain_waiting in_flight=%s", self.in_flight())
            time.sleep(poll_s)
        idle = self.in_flight() == 0
        if idle:
            logger.info("drain_complete in_flight=0")
        else:
            logger.warning("drain_timeout in_flight=%s", self.in_flight())
        return idle

    def snapshot(self) -> dict[str, Any]:
        return {
            "draining": self.draining,
            "in_flight": self.in_flight(),
            "cap_s": self.cap_s,
        }


_DRAIN = DrainController()


def get_drain() -> DrainController:
    return _DRAIN
