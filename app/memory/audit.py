"""Append-only audit log helpers (Sprint 1/6)."""

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.state import Event


class AuditRecord(BaseModel):
    """Append-only audit element, structured for later Postgres export."""

    audit_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    ts: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    call_id: str
    borrower_id: str
    tenant_id: str = ""
    event: Event
    export_schema_version: int = 3

    def to_json(self) -> str:
        return self.model_dump_json()


def audit_key(borrower_id: str) -> str:
    return f"audit:{borrower_id}"


def build_audit_record(
    event: Event,
    *,
    call_id: str,
    borrower_id: str,
    tenant_id: str = "",
    audit_id: str | None = None,
) -> AuditRecord:
    record = AuditRecord(
        call_id=call_id,
        borrower_id=borrower_id,
        tenant_id=tenant_id,
        event=event,
    )
    if audit_id is not None:
        record.audit_id = audit_id
    if not event.ts:
        record.event = event.model_copy(update={"ts": record.ts})
    return record


def parse_audit_records(raw_items: list[str]) -> list[AuditRecord]:
    records: list[AuditRecord] = []
    for item in raw_items:
        records.append(AuditRecord.model_validate_json(item))
    return records


class TurnAuditChain(BaseModel):
    """Full reconstructable chain for one engine turn."""

    audit_id: str
    call_id: str
    borrower_id: str
    tenant_id: str = ""
    ts: str = ""
    candidate_flows: list[dict[str, Any]] = Field(default_factory=list)
    commands: list[dict[str, Any]] = Field(default_factory=list)
    actions_called: list[str] = Field(default_factory=list)
    safety_preempted: bool = False
    safety_reason: str | None = None
    gate_verdict: str = ""
    gate_level: str = ""
    gate_reason: str = ""
    final_reply: str = ""
    transfer_to_human: bool = False
    latency_ms: dict[str, float] = Field(default_factory=dict)
    engine_internal_ms: float = 0.0
    external_ms: float = 0.0
    llm_calls: int = 0
    recovery: dict[str, Any] = Field(default_factory=dict)
    reply_id: str | None = None
    variant_index: int | None = None
    language: str | None = None
    tone_register: str | None = None
    agent_id: str | None = None
    pack_id: str | None = None
    manifest_version: str | None = None


def build_turn_audit_event(chain: TurnAuditChain) -> Event:
    return Event(
        ts=chain.ts or datetime.now(UTC).isoformat(),
        kind="turn_audit",
        data=chain.model_dump(mode="json"),
    )


def build_turn_audit_record(
    chain: TurnAuditChain,
    *,
    audit_id: str | None = None,
) -> AuditRecord:
    event = build_turn_audit_event(chain)
    return build_audit_record(
        event,
        call_id=chain.call_id,
        borrower_id=chain.borrower_id,
        tenant_id=chain.tenant_id,
        audit_id=audit_id or chain.audit_id,
    )


def parse_turn_audit_chain(record: AuditRecord) -> TurnAuditChain | None:
    if record.event.kind != "turn_audit":
        return None
    return TurnAuditChain.model_validate(record.event.data)


def parse_turn_audit_chains(raw_items: list[str]) -> list[TurnAuditChain]:
    chains: list[TurnAuditChain] = []
    for record in parse_audit_records(raw_items):
        parsed = parse_turn_audit_chain(record)
        if parsed is not None:
            chains.append(parsed)
    return chains


async def query_turn_audits_by_borrower(
    memory: Any,
    borrower_id: str,
) -> list[TurnAuditChain]:
    raw_items = await memory.list_audit(borrower_id)
    return parse_turn_audit_chains(raw_items)
