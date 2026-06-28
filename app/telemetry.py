"""OpenTelemetry tracing for the turn decision chain (Sprint 7)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.engine.latency import TurnLatencyProfile
from app.memory.audit import TurnAuditChain

logger = logging.getLogger(__name__)

try:
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode, Tracer

    _TRACER: Tracer | None = trace.get_tracer("collections-engine", "0.1.0")
    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised when otel not installed
    _TRACER = None
    _OTEL_AVAILABLE = False


class _NoOpSpan:
    def set_attribute(self, key: str, value: Any) -> None:
        _ = key, value

    def set_status(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def end(self) -> None:
        return None

    def __enter__(self) -> _NoOpSpan:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


@contextmanager
def span(
    name: str,
    *,
    attributes: dict[str, Any] | None = None,
    external: bool = False,
) -> Iterator[Any]:
    """Start a child span (or no-op when OpenTelemetry is unavailable)."""
    if _TRACER is None:
        yield _NoOpSpan()
        return

    attrs = dict(attributes or {})
    if external:
        attrs["latency.class"] = "external"
    with _TRACER.start_as_current_span(name) as active:
        for key, value in attrs.items():
            active.set_attribute(key, value)
        yield active


def otel_enabled() -> bool:
    return _OTEL_AVAILABLE


def annotate_turn_span(
    span_obj: Any,
    *,
    chain: TurnAuditChain | None = None,
    latency: TurnLatencyProfile | None = None,
    llm_calls: int = 0,
) -> None:
    if chain is not None:
        span_obj.set_attribute("turn.audit_id", chain.audit_id)
        span_obj.set_attribute("turn.call_id", chain.call_id)
        span_obj.set_attribute("turn.borrower_id", chain.borrower_id)
        span_obj.set_attribute(
            "turn.candidate_flows",
            ",".join(str(f.get("name", "")) for f in chain.candidate_flows),
        )
        span_obj.set_attribute("turn.commands", str(chain.commands))
        span_obj.set_attribute("turn.actions_called", ",".join(chain.actions_called))
        span_obj.set_attribute("turn.gate_verdict", chain.gate_verdict)
        span_obj.set_attribute("turn.gate_level", chain.gate_level)
        span_obj.set_attribute("turn.safety_preempted", chain.safety_preempted)
        span_obj.set_attribute("turn.transfer_to_human", chain.transfer_to_human)
    if latency is not None:
        span_obj.set_attribute("latency.engine_internal_ms", latency.engine_internal_ms)
        span_obj.set_attribute("latency.external_ms", latency.external_ms)
        span_obj.set_attribute("latency.total_ms", latency.total_ms)
    span_obj.set_attribute("turn.llm_calls", llm_calls)


@contextmanager
def turn_trace(
    call_id: str,
    borrower_id: str,
    tenant_id: str,
) -> Iterator[Any]:
    """Parent span for one /turn request."""
    if _TRACER is None:
        noop = _NoOpSpan()
        yield noop
        return

    with _TRACER.start_as_current_span("handle_turn") as active:
        active.set_attribute("call_id", call_id)
        active.set_attribute("borrower_id", borrower_id)
        active.set_attribute("tenant_id", tenant_id)
        try:
            yield active
        except Exception as exc:
            active.set_status(Status(StatusCode.ERROR, str(exc)))
            raise
