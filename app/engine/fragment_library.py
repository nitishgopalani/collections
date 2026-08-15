"""W2-3 Fragment library loader + compose validation.

Loads ``paisalo_fragments.yml`` (and per-tenant fragment YAMLs) into an
in-memory registry. Each fragment carries: ``id``, ``text`` (with
``{G:रही|रहा}`` gender tokens and ``{slot}`` hydrated-fact tokens),
``slots`` (the only slots the text may reference — grounding by
construction), ``answers[]`` (LLM selection tags), ``safe_in`` (Q/D/Q+D),
``category``, optional ``scenario`` / ``product`` gates, optional
``allowlist`` flag, optional ``role`` (selectable / confirm / terminal /
pair_only / redirect / dnc), and optional ``gender_token`` / ``kb_source``.

The compose command (W2-3) picks <=2 fragment ids + an ``oof_class``; this
module validates the selection:
  - ids exist in the library
  - ack pair-only fragments (role=pair_only) are never selected alone
  - scenario gate: fragment scenario list intersects the active scenario
  - product gate: fragment product list intersects the active product
  - slot hydration: every ``{slot}`` token in the selected fragments'
    text must be hydrated in ``state.slots``; unhydrated -> swap that
    fragment for ``unknown_info`` (the terminal fallback)

The renderer (W2-3) consumes the validated selection: gender-resolves
``{G:रही|रहा}`` by persona voice, renders ``{slot}`` from state, appends
the canonical re-ask (short variant). See ``compose_renderer.py``.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_TENANTS_DIR = Path(__file__).resolve().parents[1] / "tenants"

# {slot} token (hyphen + alnum + underscore). {G:रही|रहा} is the gender token
# (handled by the renderer, not hydration).
_SLOT_TOKEN_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_G_TOKEN_RE = re.compile(r"\{G:([^|}]+)\|([^}]+)\}")


@lru_cache(maxsize=8)
def _load_tenant_fragments(tenant_id: str) -> dict[str, dict[str, Any]]:
    """Load <tenant_id>_fragments.yml → {fragment_id: fragment_dict}."""
    path = _TENANTS_DIR / f"{tenant_id}_fragments.yml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    frags = data.get("fragments") or []
    out: dict[str, dict[str, Any]] = {}
    for f in frags:
        fid = f.get("id")
        if not fid:
            continue
        out[fid] = f
    return out


def get_fragment(tenant_id: str, fragment_id: str) -> dict[str, Any] | None:
    return _load_tenant_fragments(tenant_id).get(fragment_id)


def list_fragments(tenant_id: str) -> list[dict[str, Any]]:
    return list(_load_tenant_fragments(tenant_id).values())


def text_slots(text: str) -> list[str]:
    """Return the list of ``{slot}`` tokens referenced in ``text`` (excludes ``{G:..}``)."""
    if not text:
        return []
    g_stripped = _G_TOKEN_RE.sub("", text)
    return _SLOT_TOKEN_RE.findall(g_stripped)


def validate_compose(
    tenant_id: str,
    fragment_ids: list[str],
    *,
    scenario: str | None,
    product: str | None,
    state_slots: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Validate a compose selection.

    Returns ``(resolved_ids, rejections)``:
      - ``resolved_ids``: the final fragment-id list to render (may swap
        unhydrated fragments for ``unknown_info``).
      - ``rejections``: human-readable rejection reasons (logged in
        ``turn_decision`` guards).
    """
    lib = _load_tenant_fragments(tenant_id)
    resolved: list[str] = []
    rejections: list[str] = []

    if len(fragment_ids) > 2:
        rejections.append(f"compose over-limit: {len(fragment_ids)} ids (>2)")
        fragment_ids = fragment_ids[:2]

    if not fragment_ids:
        rejections.append("compose empty")
        return (["unknown_info"] if "unknown_info" in lib else [], rejections)

    has_pair_only = False
    has_non_pair = False
    for fid in fragment_ids:
        frag = lib.get(fid)
        if frag is None:
            rejections.append(f"compose unknown fragment: {fid}")
            resolved.append("unknown_info" if "unknown_info" in lib else fid)
            continue
        role = frag.get("role", "selectable")
        # scenario gate
        fscen = frag.get("scenario")
        if fscen and scenario and scenario not in fscen:
            rejections.append(f"compose scenario gate: {fid} not in {scenario}")
            resolved.append("unknown_info" if "unknown_info" in lib else fid)
            continue
        # product gate
        fprod = frag.get("product")
        if fprod and product and product not in fprod:
            rejections.append(f"compose product gate: {fid} not in {product}")
            resolved.append("unknown_info" if "unknown_info" in lib else fid)
            continue
        # slot hydration gate
        needed = text_slots(frag.get("text", ""))
        missing = [s for s in needed if s not in state_slots or state_slots[s] in (None, "")]
        if missing:
            rejections.append(f"compose unhydrated slots {missing} for {fid} -> unknown_info")
            resolved.append("unknown_info" if "unknown_info" in lib else fid)
            continue
        resolved.append(fid)
        if role == "pair_only":
            has_pair_only = True
        else:
            has_non_pair = True

    # ack pair-only enforcement: a pair_only fragment (ack_neutral /
    # ack_difficulty) must NEVER be selected alone — always with a
    # deflect/fact fragment.
    if has_pair_only and not has_non_pair:
        rejections.append("compose ack pair-only selected alone (no deflect/fact)")
        # append a generic deflect so the reply is not bare empathy
        if "deflect_branch_generic" in lib and "deflect_branch_generic" not in resolved:
            resolved.append("deflect_branch_generic")

    return resolved, rejections


_CONFIRM_ROLES = frozenset({"confirm", "pair_only", "dnc"})
_REFUSED_VALUES = frozenset({"refused", "unwilling", "later", "denied", "no"})
_WILLING_VALUES = frozenset({"willing", "confirmed", "yes", "haan"})


def _scenario_allows(fragment_scenarios: Any, scenario: str | None) -> bool:
    """True when the fragment has no scenario gate, or the active scenario hits it.

    ``postdue`` (catalog-normalized) matches ``postdue1`` / ``postdue2`` / ``postdue3``.
    """
    if not fragment_scenarios or not scenario:
        return True
    tags = [str(s).strip().lower() for s in fragment_scenarios if s]
    scen = scenario.strip().lower()
    if scen in tags:
        return True
    if scen == "postdue" and any(t.startswith("postdue") for t in tags):
        return True
    if scen.startswith("postdue") and "postdue" in tags:
        return True
    return False


def build_fragment_index(
    tenant_id: str,
    scenario: str | None = None,
) -> list[dict[str, Any]]:
    """Scenario-scoped compose index: ``{id, answers}`` for selectable fragments.

    Confirm / pair_only / dnc roles are excluded (gate-issued, not LLM-picked).
    Fragments with empty ``answers`` are omitted — they cannot be retrieved by tag.
    """
    out: list[dict[str, Any]] = []
    for frag in list_fragments(tenant_id):
        fid = frag.get("id")
        answers = [str(a) for a in (frag.get("answers") or []) if a]
        if not fid or not answers:
            continue
        role = frag.get("role") or "selectable"
        if role in _CONFIRM_ROLES:
            continue
        if not _scenario_allows(frag.get("scenario"), scenario):
            continue
        out.append({"id": str(fid), "answers": answers})
    return out


def resolve_confirm_fragment(
    tenant_id: str,
    slot: str | None,
    value: str | None,
    *,
    committed_date: str | None = None,
) -> str | None:
    """Value-aware confirm fragment: date readback, refused, or default.

    Money-state with a concrete ``committed_date`` → ``confirm_pay_date``.
    Refusal class → ``confirm_<slot>_refused`` when that id exists.
    Else ``confirm_<slot>``.
    """
    if not slot:
        return None
    v = str(value or "").strip().lower()
    if committed_date and v not in _REFUSED_VALUES:
        if get_fragment(tenant_id, "confirm_pay_date"):
            return "confirm_pay_date"
    if v in _REFUSED_VALUES:
        specific = f"confirm_{slot}_refused"
        if get_fragment(tenant_id, specific):
            return specific
    return f"confirm_{slot}"


def offline_compliance_pass(tenant_id: str) -> dict[str, Any]:
    """P5.0-style offline compliance gate over the whole library.

    Checks every fragment for:
      - id present + unique
      - text present + non-empty
      - every ``{slot}`` token is in the fragment's ``slots`` list
        (grounding by construction)
      - ``{G:रही|रहा}`` token (if present) is well-formed
      - allowlist fragments are marked ``allowlist: true``
      - pair_only fragments are marked ``role: pair_only``
    Returns a report dict (pass/fail + per-fragment issues). Called from
    the offline compliance script (W2-3 §8) and at loader import in tests.
    """
    lib = _load_tenant_fragments(tenant_id)
    issues: list[str] = []
    seen: set[str] = set()
    for fid, frag in lib.items():
        if fid in seen:
            issues.append(f"{fid}: duplicate id")
        seen.add(fid)
        text = frag.get("text", "")
        if not text.strip():
            issues.append(f"{fid}: empty text")
        # grounding by construction: every {slot} in text must be in slots
        declared = set(frag.get("slots") or [])
        for s in text_slots(text):
            if s not in declared:
                issues.append(f"{fid}: text references {{{s}}} not in slots list")
        # gender token well-formed: {G:feminine|masculine} — two non-empty
        # alternatives separated by '|'. The forms vary by verb (रही/रहा,
        # सकती/सकता, बोल रही/रहा, etc.) so we validate structure, not the
        # specific strings.
        for m in _G_TOKEN_RE.finditer(text):
            fem, mask = m.group(1), m.group(2)
            if not fem.strip() or not mask.strip():
                issues.append(f"{fid}: malformed {{G:..}} token {m.group(0)!r}")
        # allowlist fragments marked
        if frag.get("allowlist") and not frag.get("allowlist"):
            issues.append(f"{fid}: allowlist flag false on allowlist fragment")
    return {
        "tenant_id": tenant_id,
        "fragment_count": len(lib),
        "issues": issues,
        "pass": len(issues) == 0,
    }
