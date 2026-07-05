"""Incremental sentence-boundary splitter for streamed LLM tokens.

Feeds prompt-mode streaming (app/engine/prompt_agent.handle_prompt_turn_streaming):
tokens go in as they arrive from the LLM, complete sentences come out as soon
as their boundary is confirmed, so the first sentence can reach TTS while the
model is still generating the rest.

Rules:

* Boundaries: ``. ! ?``, Hindi danda ``।`` and double danda ``॥``, plus
  newlines. A boundary only fires once the terminator is followed by
  whitespace — a terminator at the very end of the buffer waits for the next
  token (it may turn out to be ``3.5`` or ``Rs.500``); the tail is emitted by
  :meth:`SentenceStreamSplitter.flush` when the stream ends.
* Numbers: ``.`` with a non-space character after it never splits (decimals,
  versions, IDs like ``BK.123``).
* Abbreviations: ``.`` after a known abbreviation (``Rs.``, ``Dr.``, ``no.``,
  ...) or a single letter (initials) never splits.
* Angle-bracket markers (``<consult ...>`` / ``<consult_result ...>``) may be
  split across token boundaries by the LLM: from an unclosed ``<`` until its
  ``>`` the splitter refuses to split, so a marker always lands whole inside
  one sentence and the existing marker regexes keep working per sentence.
"""

from __future__ import annotations

import re

_TERMINATORS = ".!?।॥"
# Characters allowed to trail a terminator and still belong to the sentence.
_TRAILERS = _TERMINATORS + "\"'”’)»"

# Lower-cased words whose trailing '.' is an abbreviation, not a boundary.
_ABBREVIATIONS = frozenset(
    {
        "mr",
        "mrs",
        "ms",
        "dr",
        "prof",
        "rs",
        "re",
        "no",
        "nos",
        "vs",
        "etc",
        "approx",
        "min",
        "max",
        "st",
        "jr",
        "sr",
        "dept",
        "govt",
    }
)

_ABBREV_TAIL_RE = re.compile(r"([A-Za-z\u0900-\u097F]+)\.$")


def _dot_ends_abbreviation(prefix: str) -> bool:
    """True when ``prefix`` (ending in '.') ends with an abbreviation/initial."""
    m = _ABBREV_TAIL_RE.search(prefix)
    if m is None:
        return False
    word = m.group(1)
    return len(word) == 1 or word.lower() in _ABBREVIATIONS


class SentenceStreamSplitter:
    """Stateful splitter: push tokens, get back completed sentences."""

    def __init__(self) -> None:
        self._buf = ""

    @property
    def pending(self) -> str:
        """Text buffered but not yet confirmed as a complete sentence."""
        return self._buf

    def push(self, token: str) -> list[str]:
        """Add one token; return sentences completed by it (possibly none)."""
        if not token:
            return []
        self._buf += token
        return self._drain()

    def flush(self) -> list[str]:
        """Stream ended: return whatever remains as the final sentence."""
        rest = self._buf.strip()
        self._buf = ""
        return [rest] if rest else []

    def _drain(self) -> list[str]:
        out: list[str] = []
        while True:
            cut = self._next_boundary()
            if cut is None:
                break
            sentence = self._buf[:cut].strip()
            self._buf = self._buf[cut:].lstrip()
            if sentence:
                out.append(sentence)
        return out

    def _next_boundary(self) -> int | None:
        buf = self._buf
        in_marker = False
        for i, ch in enumerate(buf):
            if ch == "<":
                in_marker = True
                continue
            if ch == ">":
                in_marker = False
                continue
            if in_marker:
                # Inside a (possibly still-open) <...> marker: never split. If
                # the '>' hasn't streamed in yet we wait for more tokens.
                continue
            if ch == "\n":
                return i + 1
            if ch not in _TERMINATORS:
                continue
            j = i + 1
            while j < len(buf) and buf[j] in _TRAILERS:
                j += 1
            if j >= len(buf):
                # Terminator run touches the buffer end — the next token
                # decides (decimal continuation vs. real boundary).
                return None
            if not buf[j].isspace():
                continue  # e.g. "3.5", "v2.1", "BK.123"
            if ch == "." and _dot_ends_abbreviation(buf[: i + 1]):
                continue
            return j
        return None
