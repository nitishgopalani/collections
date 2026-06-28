"""Scripted KB/LLM clients for local flow simulation."""

import json
from typing import Any


class ScriptedKB:
    def __init__(self, results: list[dict[str, Any]]) -> None:
        self._results = results
        self.retrieve_calls = 0

    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def retrieve(self, text: str, tenant_id: str, k: int = 6) -> list[dict[str, Any]]:
        self.retrieve_calls += 1
        _ = text, tenant_id, k
        return list(self._results)


class ScriptedLLM:
    def __init__(self, responses: list[str | list[dict[str, Any]]]) -> None:
        self._responses = [json.dumps(r) if isinstance(r, list) else r for r in responses]
        self.call_count = 0

    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def complete(self, system: str, user: str, *, json_only: bool = True) -> str:
        self.call_count += 1
        if self.call_count <= len(self._responses):
            return self._responses[self.call_count - 1]
        return "[]"
