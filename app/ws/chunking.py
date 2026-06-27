"""Split gated replies into TTS-friendly sentence chunks (EB-6)."""

import re

# Split after sentence terminators (Latin + Hindi danda) or newlines.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?।])\s+|\n+")


def chunk_reply_for_tts(text: str) -> list[str]:
    """Return non-empty sentence chunks suitable for incremental TTS playback."""
    cleaned = text.strip()
    if not cleaned:
        return []
    parts = _SENTENCE_BOUNDARY.split(cleaned)
    chunks = [part.strip() for part in parts if part.strip()]
    return chunks if chunks else [cleaned]
