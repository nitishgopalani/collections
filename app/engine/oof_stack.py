"""OOF-STACK: L0 deterministic topics + L1 router related/ack + recovery index.

Invariant #9: tenant packs load from ``{tenant_id}_irrelevant_topics.yml``.
No tenant string-compares.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.engine.compliance_rules import normalize
from app.engine.compose_renderer import render_compose, render_unrelated_redirect
from app.engine.fragment_library import list_fragments
from app.engine.scripted_coercions import _tokenize
from app.schemas.command import Command
from app.schemas.state import ConversationState

logger = logging.getLogger(__name__)

_TENANTS_DIR = Path(__file__).resolve().parents[1] / "tenants"

FALLBACK_ACK = "आप शायद किसी और बात के बारे में पूछ रहे हैं।"
_REGISTER_RE = re.compile(r"आप\s+शायद.+के\s+बारे", re.UNICODE)
_DIGIT_RE = re.compile(r"\d")
_NAME_SLOT_KEYS = ("customer_name", "borrower_name")


@dataclass(frozen=True)
class L0Hit:
    subclass: str
    ack: str


@lru_cache(maxsize=16)
def load_irrelevant_topics(tenant_id: str) -> dict[str, Any]:
    if not tenant_id:
        return {}
    path = _TENANTS_DIR / f"{tenant_id}_irrelevant_topics.yml"
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return raw if isinstance(raw, dict) else {}


def clear_irrelevant_topics_cache() -> None:
    load_irrelevant_topics.cache_clear()


def _cue_hits(norm: str, tokens: set[str], cue: str) -> bool:
    c = normalize(cue)
    if not c:
        return False
    if " " in c:
        return c in norm
    if len(c) <= 3:
        return c in tokens
    return c in tokens or c in norm


def match_l0(tenant_id: str, transcript: str) -> L0Hit | None:
    """Devanagari-aware L0 match. Hit → no LLM."""
    text = (transcript or "").strip()
    if not text:
        return None
    packs = (load_irrelevant_topics(tenant_id).get("packs") or {})
    if not isinstance(packs, dict):
        return None
    norm = normalize(text)
    tokens = {normalize(t) for t in _tokenize(text)}
    for subclass, spec in packs.items():
        if not isinstance(spec, dict):
            continue
        cues = spec.get("cues") or []
        if any(_cue_hits(norm, tokens, str(c)) for c in cues if c):
            ack = str(spec.get("ack") or FALLBACK_ACK).strip()
            return L0Hit(subclass=str(subclass), ack=ack)
    return None


def guard_ack_text(ack_text: str | None, slots: dict[str, Any]) -> tuple[str, bool]:
    """Register + ≤12 words + no names/numbers. Fail → fallback + ack_dropped."""
    text = (ack_text or "").strip()
    dropped = False
    if not text or not _REGISTER_RE.search(text):
        text, dropped = FALLBACK_ACK, True
    elif len(text.split()) > 12:
        text, dropped = FALLBACK_ACK, True
    elif _DIGIT_RE.search(text):
        text, dropped = FALLBACK_ACK, True
    else:
        for key in _NAME_SLOT_KEYS:
            name = str(slots.get(key) or "").strip()
            if len(name) >= 2 and name in text:
                text, dropped = FALLBACK_ACK, True
                break
    return text, dropped


def sweep_recovery_index(tenant_id: str, transcript: str) -> str | None:
    """Fragment trigger_synonyms + answers[] (flow cues). Longest cue wins."""
    text = (transcript or "").strip()
    if not text:
        return None
    norm = normalize(text)
    tokens = {normalize(t) for t in _tokenize(text)}
    best: tuple[int, str] | None = None
    for frag in list_fragments(tenant_id):
        fid = str(frag.get("id") or "")
        if not fid:
            continue
        cues = [str(c) for c in (frag.get("trigger_synonyms") or []) if c]
        cues.extend(str(a) for a in (frag.get("answers") or []) if a)
        for cue in cues:
            c = normalize(cue)
            if not c or not _cue_hits(norm, tokens, cue):
                continue
            score = len(c)
            if best is None or score > best[0]:
                best = (score, fid)
    return best[1] if best else None


def apply_l1(
    parse_result: Any,
    transcript: str,
    tenant_id: str,
    state: ConversationState,
) -> None:
    """Mutate parse_result + state slots for the L1 router contract."""
    related = getattr(parse_result, "related", None)
    oof = getattr(parse_result, "oof_class", None)
    if related is None and oof != "irrelevant":
        return
    if related is None:
        related = False
    parse_result.related = related
    state.slots["oof_layer"] = "llm"
    state.slots["related"] = related

    if related is True:
        fid = sweep_recovery_index(tenant_id, transcript)
        if fid:
            parse_result.commands = [
                Command(
                    command="compose",
                    fragments=[fid],
                    oof_class="call_context",
                )
            ]
            parse_result.oof_class = "call_context"
            state.slots["recovered_via"] = "index"
            state.slots["_oof_recovered_fid"] = fid
            state.slots.pop("_oof_body", None)
            state.slots.pop("_oof_ack", None)
            logger.info("oof_l1 recovered_via=index fragment=%s", fid)
            return
        state.slots["related_miss"] = True
        state.slots["_oof_body"] = "honest_miss"
        parse_result.oof_class = parse_result.oof_class or "related_oof"
        ack, dropped = guard_ack_text(getattr(parse_result, "ack_text", None), state.slots)
        state.slots["_oof_ack"] = ack
        if dropped:
            state.slots["ack_dropped"] = True
        logger.info("oof_l1 related_miss transcript=%s", (transcript or "")[:80])
        return

    parse_result.oof_class = "irrelevant"
    state.slots["_oof_body"] = "boundary"
    ack, dropped = guard_ack_text(getattr(parse_result, "ack_text", None), state.slots)
    state.slots["_oof_ack"] = ack
    if dropped:
        state.slots["ack_dropped"] = True


def render_oof_turn(
    tenant_id: str,
    state: ConversationState,
    *,
    identity_ok: bool,
    persona_voice: str | None,
) -> str:
    """Identical L0/L1 shape: optional first-diversion ack + body. Re-ask is appended by turn.py."""
    first = int(state.slots.get("_redirect_count") or 0) == 0
    parts: list[str] = []
    ack = str(state.slots.get("_oof_ack") or "").strip()
    if first and ack:
        parts.append(ack)
    body = str(state.slots.get("_oof_body") or "boundary")
    slots = dict(state.slots)
    if body == "honest_miss":
        spoken = render_compose(
            tenant_id, ["honest_miss_deflect"], slots, persona_voice=persona_voice
        )
        if not spoken:
            spoken = render_unrelated_redirect(
                tenant_id,
                identity_ok=identity_ok,
                state_slots=slots,
                persona_voice=persona_voice,
            )
        parts.append(spoken)
    else:
        parts.append(
            render_unrelated_redirect(
                tenant_id,
                identity_ok=identity_ok,
                state_slots=slots,
                persona_voice=persona_voice,
            )
        )
    return " ".join(p for p in parts if p)
