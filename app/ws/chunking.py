"""Split gated replies into TTS-friendly sentence chunks (EB-6)."""

import re

# Split after sentence terminators (Latin + Hindi danda) or newlines.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?।])\s+|\n+")


_COMPLIANCE_PREFIX = "[COMPLIANCE-REVIEW]"


def _strip_compliance_prefix(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith(_COMPLIANCE_PREFIX):
        return cleaned[len(_COMPLIANCE_PREFIX) :].strip()
    return cleaned


def chunk_reply_for_tts(text: str, slots: dict | None = None) -> list[str]:
    """Return non-empty sentence chunks suitable for incremental TTS playback.

    DEBT-038: when ``slots`` is provided, split around hydrated names first so
    static prefix/suffix become their own TTS cache keys.
    """
    cleaned = _strip_compliance_prefix(text)
    if not cleaned:
        return []
    from app.engine.tts_segments import segment_spoken_reply

    segments = segment_spoken_reply(cleaned, slots) if slots else [cleaned]
    chunks: list[str] = []
    for segment in segments:
        parts = _SENTENCE_BOUNDARY.split(segment)
        chunks.extend(part.strip() for part in parts if part.strip())
    return chunks if chunks else [cleaned]
