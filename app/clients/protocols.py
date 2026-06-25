from typing import Any, Protocol

from app.schemas.state import BorrowerRecord, ConversationState, Event


class LLMClient(Protocol):
    async def complete(self, system: str, user: str, *, json_only: bool = True) -> str: ...


class KBClient(Protocol):
    async def retrieve(self, text: str, tenant_id: str, k: int = 6) -> list[dict[str, Any]]: ...


class ToolClient(Protocol):
    async def invoke(
        self,
        tool: str,
        args: dict[str, Any],
        tenant_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]: ...


class MemoryStore(Protocol):
    async def load_state(self, call_id: str) -> ConversationState | None: ...
    async def save_state(self, state: ConversationState) -> None: ...
    async def load_borrower(self, borrower_id: str) -> BorrowerRecord | None: ...
    async def save_borrower(self, record: BorrowerRecord) -> None: ...
    async def append_audit(
        self,
        event: Event,
        *,
        call_id: str,
        borrower_id: str,
        tenant_id: str = "",
    ) -> str: ...


class ClientHealth(Protocol):
    async def ping(self) -> bool: ...
