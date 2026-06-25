"""Per-turn latency profiling (Sprint 7).

Engine-internal stages (safety, tracker, priority, executor, nlg, gate, persist) count
toward the p95 ≤ 700 ms target. External network stages (KB retrieval, Gemini LLM,
tool HTTP) are reported separately — unavoidable external latency is not counted against
the engine-internal budget but is surfaced for ops.
"""

from dataclasses import dataclass, field
from time import perf_counter


@dataclass
class StageTiming:
    name: str
    duration_ms: float
    external: bool = False


@dataclass
class TurnLatencyProfile:
    stages: list[StageTiming] = field(default_factory=list)

    def record(self, name: str, duration_ms: float, *, external: bool = False) -> None:
        self.stages.append(StageTiming(name=name, duration_ms=duration_ms, external=external))

    @property
    def engine_internal_ms(self) -> float:
        return sum(s.duration_ms for s in self.stages if not s.external)

    @property
    def external_ms(self) -> float:
        return sum(s.duration_ms for s in self.stages if s.external)

    @property
    def total_ms(self) -> float:
        return self.engine_internal_ms + self.external_ms

    def stage_ms(self) -> dict[str, float]:
        return {s.name: s.duration_ms for s in self.stages}

    def to_dict(self) -> dict[str, float | dict[str, float]]:
        return {
            "engine_internal_ms": round(self.engine_internal_ms, 2),
            "external_ms": round(self.external_ms, 2),
            "total_ms": round(self.total_ms, 2),
            "stages": {s.name: round(s.duration_ms, 2) for s in self.stages},
        }


class StageTimer:
    """Context manager that records one stage on a TurnLatencyProfile."""

    def __init__(self, profile: TurnLatencyProfile, name: str, *, external: bool = False) -> None:
        self._profile = profile
        self._name = name
        self._external = external
        self._start: float = 0.0

    def __enter__(self) -> "StageTimer":
        self._start = perf_counter()
        return self

    def __exit__(self, *_args: object) -> None:
        elapsed_ms = (perf_counter() - self._start) * 1000
        self._profile.record(self._name, elapsed_ms, external=self._external)
