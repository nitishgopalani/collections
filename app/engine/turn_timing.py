"""Per-turn latency instrumentation for prompt-mode turns.

One PromptTurnTiming instance per turn, created by the WS handler when the
TurnMessage arrives and threaded through the prompt agent. Marks are
first-write-wins (a retried stage keeps its earliest timestamp). The handler
emits ONE structured log line per turn via :meth:`log_line`; the go-server
logs its own absolute timestamps for the same session_id + turn_id, so the
two lines join into the full mouth-to-ear breakdown:

    asr_final_ts (go) -> turn_received (brain) -> llm_start -> llm_first_token
    -> first_sentence_complete -> first_chunk_sent (brain) -> engine_first_chunk
    -> tts_first_audio -> egress_first_frame (go)

All *_ms fields in the log line are relative to turn_received; -1 means the
stage never happened (e.g. hold short-circuits skip the LLM entirely).
turn_received_ts_ms is absolute epoch milliseconds for cross-service joins.
"""

from __future__ import annotations

import time

# Stage names (relative marks; turn_received is the zero point).
STAGE_LLM_START = "llm_start"
STAGE_LLM_FIRST_TOKEN = "llm_first_token"
STAGE_FIRST_SENTENCE = "first_sentence_complete"
STAGE_FIRST_CHUNK_SENT = "first_chunk_sent"
STAGE_LLM_DONE = "llm_done"
STAGE_TURN_DONE = "turn_done"


class PromptTurnTiming:
    """Wall-clock stage marks for one prompt-mode turn."""

    __slots__ = ("session_id", "turn_id", "path", "_t0", "_marks")

    def __init__(self, session_id: str, turn_id: str) -> None:
        self.session_id = session_id
        self.turn_id = turn_id
        self.path = "unknown"  # streaming | buffered | hold | relay
        self._t0 = time.monotonic()
        self._marks: dict[str, float] = {}

    def mark(self, stage: str) -> None:
        """Record a stage once (first mark wins)."""
        self._marks.setdefault(stage, time.monotonic())

    def set_path(self, path: str) -> None:
        self.path = path

    def _rel_ms(self, stage: str) -> int:
        at = self._marks.get(stage)
        if at is None:
            return -1
        return int((at - self._t0) * 1000)

    def log_line(self) -> str:
        """The single per-turn breakdown line (space-separated key=value)."""
        turn_received_epoch_ms = int(time.time() * 1000 - (time.monotonic() - self._t0) * 1000)
        fields = [
            ("session_id", self.session_id),
            ("turn_id", self.turn_id),
            ("path", self.path),
            ("turn_received_ts_ms", turn_received_epoch_ms),
            ("llm_start_ms", self._rel_ms(STAGE_LLM_START)),
            ("llm_first_token_ms", self._rel_ms(STAGE_LLM_FIRST_TOKEN)),
            ("first_sentence_complete_ms", self._rel_ms(STAGE_FIRST_SENTENCE)),
            ("first_chunk_sent_ms", self._rel_ms(STAGE_FIRST_CHUNK_SENT)),
            ("llm_done_ms", self._rel_ms(STAGE_LLM_DONE)),
            ("total_ms", self._rel_ms(STAGE_TURN_DONE)),
        ]
        return "prompt_turn_latency " + " ".join(f"{k}={v}" for k, v in fields)
