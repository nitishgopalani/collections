"""Persona-voice gender tokens — shared by NLG, compose, and system copy.

``{G:feminine|masculine}`` is position-based (group 1 = feminine). Bare
``{G}`` expands to रही / रहा. Unknown voice defaults feminine (PaisaLo
default persona is priya/neha).
"""

from __future__ import annotations

import re

_G_TOKEN_RE = re.compile(r"\{G:([^|}]+)\|([^}]+)\}")
_G_BARE_RE = re.compile(r"\{G\}")

FEMININE_VOICES = frozenset({"priya", "neha", "simran", "anushka"})
MASCULINE_VOICES = frozenset({"kabir", "amit", "arjun", "rajveer"})

# First-person gendered verbs that must be wrapped in {G:fem|mask} when saved.
_GENDERED_VERB_RE = re.compile(
    r"(?:पा रहा|पा रही|कर रहा|कर रही|बोल रहा|बोल रही|"
    r"समझ रहा|समझ रही|दे रहा|दे रही|ले रहा|ले रही|"
    r"रहा हूँ|रही हूँ|सकता|सकती|दूंगा|दूँगा|दूंगी|दूँगी|"
    r"लूंगा|लूँगा|लूंगी|लूँगी|पाया|पाई)"
)


def is_feminine_voice(persona_voice: str | None) -> bool:
    if not persona_voice:
        return True
    key = persona_voice.lower()
    if key in MASCULINE_VOICES:
        return False
    if key in FEMININE_VOICES:
        return True
    return True


def resolve_gender_tokens(text: str, persona_voice: str | None) -> str:
    """Replace every ``{G:fem|mask}`` / ``{G}`` token by persona voice."""
    feminine = is_feminine_voice(persona_voice)

    def _pick(match: re.Match[str]) -> str:
        return match.group(1) if feminine else match.group(2)

    out = _G_TOKEN_RE.sub(_pick, text or "")
    out = _G_BARE_RE.sub("रही" if feminine else "रहा", out)
    return out


def tokens_used(text: str) -> list[str]:
    """Slot names plus a ``G`` marker when a gender token is present."""
    slots = re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", text or "")
    out = [s for s in slots if s != "G"]
    if _G_TOKEN_RE.search(text or "") or _G_BARE_RE.search(text or ""):
        out.append("G")
    # unique, stable order
    seen: set[str] = set()
    ordered: list[str] = []
    for name in out:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def missing_gender_token(text: str) -> bool:
    """True when gendered first-person verbs appear without a {G} token."""
    if not (text or "").strip():
        return False
    if _G_TOKEN_RE.search(text) or _G_BARE_RE.search(text):
        return False
    return bool(_GENDERED_VERB_RE.search(text))
