import logging
from typing import Any

from app.clients.tools_live import LiveToolClient
from app.clients.tools_sim import FakeToolClient
from app.clients.tools_stub import StubToolClient
from app.config import get_settings

logger = logging.getLogger(__name__)

INVOKE_PATH = "/invoke"


def create_tool_client() -> Any:
    settings = get_settings()
    mode = (settings.tools_mode or "stub").strip().lower()
    if mode == "stub":
        return StubToolClient()
    if mode == "simulate":
        return FakeToolClient()
    if mode == "live":
        return LiveToolClient()
    logger.warning("Unknown TOOLS_MODE=%s; falling back to stub", mode)
    return StubToolClient()


# Back-compat alias for imports expecting ToolClientImpl
ToolClientImpl = LiveToolClient
