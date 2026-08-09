"""Compliance gate and safety result schemas (Sprint 6)."""

from typing import Any, Literal

from pydantic import BaseModel, Field

ComplianceLevel = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
GateVerdict = Literal["allow", "modify", "block"]


class SafetyResult(BaseModel):
    """Care-first outcome when distress/vulnerability is detected in the transcript."""

    reason: str
    reply_text: str
    transfer_to_human: bool = True
    suspend_recovery: bool = True
    # W1-C C2 (DNC/opt-out capture): policy-lane preemption that ends the call
    # gracefully after speaking the ack (outcome 7). Vulnerability lane leaves
    # this False (outcome 5 — transfer to human specialist).
    end_call: bool = False
    compliance_updates: dict[str, Any] = Field(default_factory=dict)


class GateResult(BaseModel):
    """Final compliance verdict on an outbound line."""

    verdict: GateVerdict
    text: str
    level: ComplianceLevel
    reason: str
    transfer_to_human: bool = False
    # Allowlisted pressure hits (and similar soft findings) for audit — never empty the reply.
    warnings: list[dict[str, Any]] = Field(default_factory=list)
