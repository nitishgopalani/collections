"""W2-3 Compose renderer — fragment(s) → spoken reply text.

Render pipeline:
  1. Gender-resolve ``{G:रही|रहा}`` by persona voice (priya/neha → रही,
     kabir/amit → रहा). Voice comes from the tenant profile / scenario.
  2. Render ``{slot}`` tokens from ``state.slots`` (hydrated by
     validate_compose; unhydrated slots are already swapped to unknown_info).
  3. Amounts rendered as ``{X} रुपये`` (the fragment text already carries
     ``रुपये``; the renderer just substitutes the digit value — no "45 सौ"
     spoken form).
  4. Append the canonical re-ask (short variant) — EXACT RESUME append.
     The renderer NEVER replays the last TTS buffer; it always re-renders
     from state so the reply is fresh + grounded.

This module is pure: (fragments, state, persona_voice) → reply_text.
No state mutation, no I/O.
"""

from __future__ import annotations

import re
from typing import Any

from app.engine.fragment_library import get_fragment, text_slots
from app.engine.gender import is_feminine_voice, resolve_gender_tokens

_SLOT_TOKEN_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _gender_form(persona_voice: str | None) -> str:
    """Pick the feminine or masculine form of the {G:..} token by voice."""
    return "रही" if is_feminine_voice(persona_voice) else "रहा"


def _render_slot_value(value: Any) -> str:
    """Render a slot value for substitution. Amounts stay as digits (the
    fragment text carries ``रुपये``); ISO dates become Hindi spoken form.
    """
    if value is None:
        return ""
    text = str(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        from app.engine.nlg import spoken_date_hindi

        return spoken_date_hindi(text)
    return text


def _resolve_gender(text: str, persona_voice: str | None) -> str:
    """Replace every ``{G:fem|mask}`` token with the feminine or masculine
    alternative based on persona voice. The alternatives vary by verb
    (रही/रहा, सकती/सकता, बोल रही/रहा) so we pick by position (group 1 =
    feminine, group 2 = masculine), not by string match."""
    return resolve_gender_tokens(text, persona_voice)


def render_compose(
    tenant_id: str,
    fragment_ids: list[str],
    state_slots: dict[str, Any],
    *,
    persona_voice: str | None,
) -> str:
    """Render a validated compose selection → spoken reply text.

    ``fragment_ids`` is the RESOLVED list from ``validate_compose`` (already
    swapped for unknown_info on hydration/scenario/product failures).
    Returns the joined fragment text (≤2 fragments) with ``{G}`` and
    ``{slot}`` tokens substituted. Does NOT append the re-ask — the caller
    (turn path) appends the canonical re-ask from the active flow's
    awaiting slot.
    """
    if not fragment_ids:
        return ""
    g_form = _gender_form(persona_voice)
    parts: list[str] = []
    for fid in fragment_ids[:2]:
        frag = get_fragment(tenant_id, fid)
        if frag is None:
            continue
        text = frag.get("text", "")
        # gender-resolve {G:fem|mask} by persona voice (position-based)
        text = _resolve_gender(text, persona_voice)
        # slot substitution {slot}
        for slot_name in text_slots(text):
            val = state_slots.get(slot_name)
            text = text.replace(f"{{{slot_name}}}", _render_slot_value(val))
        parts.append(text.strip())
    return " ".join(p for p in parts if p)


def render_unrelated_redirect(
    tenant_id: str,
    *,
    identity_ok: bool,
    state_slots: dict[str, Any],
    persona_voice: str | None,
) -> str:
    """W2-3 UNRELATED deterministic lane (invariant #8).

    ``oof_class=irrelevant`` → ALWAYS render a scope-boundary fragment
    (pre-identity variant names no loan details; post-identity may
    reference this loan) + canonical re-ask. World-knowledge / RAG / tools /
    Tier-3 are OFF — the "answer" for unrelated never means content.
    """
    fid = "scope_boundary_post_identity" if identity_ok else "scope_boundary_pre_identity"
    # Fall back to irrelevant_redirect if the scoped variant is missing.
    if get_fragment(tenant_id, fid) is None:
        fid = "irrelevant_redirect"
    return render_compose(
        tenant_id, [fid], state_slots, persona_voice=persona_voice
    )
