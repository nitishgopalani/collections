"""Trivial tool client for TOOLS_MODE=stub."""

from typing import Any


class StubToolClient:
    @property
    def is_stub(self) -> bool:
        return True

    @property
    def mode(self) -> str:
        return "stub"

    async def ping(self) -> bool:
        return True

    async def invoke(
        self,
        tool: str,
        args: dict[str, Any],
        tenant_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        _ = tool, args, tenant_id, idempotency_key
        return {"ok": True, "result": {}}
