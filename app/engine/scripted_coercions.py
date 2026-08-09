"""Scripted-tenant coercions — profile-driven, tenant-agnostic.

Formerly the ``_coerce_sot_*`` block in ``turn.py``. Cue lists and slot sets come
from :class:`TenantRuntimeProfile`. The shared inability regex stays here (language
level, not tenant level).
"""

from __future__ import annotations

import re
from datetime import date

from app.engine.tenant_profile import TenantRuntimeProfile
from app.schemas.command import Command
from app.schemas.flow import FlowSet
from app.schemas.state import ConversationState

# Soft inability: "nahi ... paaunga/sakta/can't" within a short window (ASR-tolerant).
# Shared across scripted tenants — not loaded from YAML.
INABILITY_RE = re.compile(
    r"(नहीं|नही|नहि|\bnahi\b|\bnahin\b|\bnhi\b|\bno\b)"
    r".{0,30}?"
    r"(पा[एऊउ]|सक[तनू]|paung|payeg|paeg|sakt|sakun|can'?t|cannot|unable|not able)",
    re.IGNORECASE | re.UNICODE | re.DOTALL,
)


def transcript_blank(transcript: str) -> bool:
    return not (transcript or "").strip()


def is_main_ladder_flow(flow_name: str, profile: TenantRuntimeProfile) -> bool:
    return any(flow_name.startswith(prefix) for prefix in profile.main_ladder_prefixes)


def prune_spurious_objection_stack(
    state: ConversationState,
    profile: TenantRuntimeProfile,
    flows: FlowSet | None = None,
) -> ConversationState:
    """Drop a stale objection frame sitting above the main offer/push ladder.

    Never prune when the objection flow is actively waiting on a collect step
    (e.g. sot_obj_link_request's ``collect: sot_link_received``).  Those frames
    are mid-interaction, not stale.
    """
    if len(state.flow_stack) < 2 or not state.slots.get("identity_ok"):
        return state
    top = state.flow_stack[-1]
    if not top.flow.startswith(profile.objection_prefix):
        return state
    if flows is not None:
        flow_def = flows.flows.get(top.flow)
        if flow_def is not None and top.step_index < len(flow_def.steps):
            if flow_def.steps[top.step_index].collect:
                return state
    if not any(is_main_ladder_flow(frame.flow, profile) for frame in state.flow_stack[:-1]):
        return state
    updated = state.model_copy(deep=True)
    updated.flow_stack = list(state.flow_stack[:-1])
    return updated


def sanitize_blank_transcript_commands(commands: list[Command]) -> list[Command]:
    """Silence/dead-air must not start a flow, clarify-loop, or free-form respond."""
    return [
        c
        for c in commands
        if c.command not in {"start_flow", "clarify", "respond"}
    ]


def dispute_flow(transcript: str, profile: TenantRuntimeProfile) -> str | None:
    """Return the transfer objection flow for a hard dispute, else None."""
    low = (transcript or "").lower()
    loan_tokens = profile.dispute_loan_tokens or ["loan"]
    has_loan = any(tok in low for tok in loan_tokens)
    theme_flows = profile.dispute_theme_flows
    if has_loan and any(d in low for d in profile.cues("dispute_never_loan")):
        return theme_flows.get("never_loan")
    if any(c in low for c in profile.cues("dispute_wrong_amount")):
        return theme_flows.get("wrong_amount")
    if any(c in low for c in profile.cues("dispute_death")):
        return theme_flows.get("death")
    if any(c in low for c in profile.cues("dispute_frozen_account")):
        return theme_flows.get("frozen_account")
    return None


def coerce_dispute(
    commands: list[Command],
    transcript: str,
    *,
    on_rails: bool,
    profile: TenantRuntimeProfile,
) -> tuple[list[Command], bool]:
    if not on_rails:
        return commands, False
    flow = dispute_flow(transcript, profile)
    if flow is None:
        return commands, False
    return [Command(command="start_flow", flow=flow)], True


def coerce_callback_request(
    commands: list[Command],
    transcript: str,
    *,
    on_rails: bool,
    profile: TenantRuntimeProfile,
) -> tuple[list[Command], bool]:
    """PLO-OOF P1: Tier-1 callback-request deflection.

    Borrower asks to be called back / is busy / has no time now → route
    verbatim to ``profile.callback_flow`` (e.g. plo_obj_callback_pd).
    Fires only on-rails and only when the profile declares a callback_flow
    and a non-empty ``callback_request`` cue pack. Cue match is substring
    on lowercased transcript (same convention as dispute / willing).
    """
    if not on_rails:
        return commands, False
    flow = (profile.callback_flow or "").strip()
    if not flow:
        return commands, False
    low = (transcript or "").lower()
    if not any(cue in low for cue in profile.cues("callback_request")):
        return commands, False
    return [Command(command="start_flow", flow=flow)], True


def coerce_push_willing(
    commands: list[Command],
    awaiting_slot: str,
    transcript: str,
    *,
    profile: TenantRuntimeProfile,
) -> tuple[list[Command], bool]:
    if awaiting_slot not in profile.push_intent_slots:
        return commands, False
    existing = next(
        (c for c in commands if c.command == "set_slot" and c.name == awaiting_slot),
        None,
    )
    if existing is not None and str(existing.value or "").lower() in {"willing", "already_paid"}:
        return commands, False
    low = (transcript or "").lower()
    if any(bad in low for bad in profile.cues("willing_disqualifiers")):
        return commands, False
    if not any(cue in low for cue in profile.cues("willing")):
        return commands, False
    kept = [
        c
        for c in commands
        if not (c.command == "set_slot" and c.name == awaiting_slot)
        and c.command != "clarify"
    ]
    kept.append(Command(command="set_slot", name=awaiting_slot, value="willing"))
    return kept, True


def coerce_payment_refusal(
    commands: list[Command],
    awaiting_slot: str,
    transcript: str,
    *,
    profile: TenantRuntimeProfile,
) -> tuple[list[Command], bool, str | None]:
    """Return ``(commands, fired, matched_via)`` where via is ``cue``|``regex``|None.

    Cue wins when both match (more specific than the shared inability regex).
    """
    if awaiting_slot not in profile.push_intent_slots:
        return commands, False, None
    low = (transcript or "").lower()
    cue_match = any(cue in low for cue in profile.cues("intent_refusal"))
    regex_match = bool(INABILITY_RE.search(transcript or ""))
    if not (cue_match or regex_match):
        return commands, False, None
    matched_via = "cue" if cue_match else "regex"
    existing = next(
        (c for c in commands if c.command == "set_slot" and c.name == awaiting_slot),
        None,
    )
    if existing is not None and str(existing.value or "").strip():
        return commands, False, None
    kept = [
        c
        for c in commands
        if not (c.command == "set_slot" and c.name == awaiting_slot)
        and c.command not in {"clarify", "start_flow"}
    ]
    kept.append(Command(command="set_slot", name=awaiting_slot, value="refused"))
    return kept, True, matched_via


def coerce_identity(
    commands: list[Command],
    awaiting_slot: str,
    transcript: str,
    *,
    profile: TenantRuntimeProfile,
) -> list[Command]:
    slot = profile.identity_slot
    if not slot or awaiting_slot != slot:
        return commands
    if any(c.command == "set_slot" and c.name == slot for c in commands):
        return commands
    low = (transcript or "").strip().lower()
    if not low:
        return commands
    tokens = set(re.findall(r"\w+", low, flags=re.UNICODE))
    if any(p in low for p in profile.cues("id_no_phrases")) or (
        tokens & profile.cue_set("id_no_tokens")
    ):
        return [Command(command="set_slot", name=slot, value="denied")]
    if any(p in low for p in profile.cues("id_yes_phrases")) or (
        tokens & profile.cue_set("id_yes_tokens")
    ):
        return [Command(command="set_slot", name=slot, value="confirmed")]
    return commands


def coerce_commit_reversal(
    commands: list[Command],
    awaiting_slot: str,
    transcript: str,
    *,
    profile: TenantRuntimeProfile,
) -> tuple[list[Command], bool]:
    if awaiting_slot not in profile.reversal_slots:
        return commands, False
    low = (transcript or "").lower()
    if not any(cue in low for cue in profile.cues("refusal")):
        return commands, False
    supplied_time = any(
        c.command == "set_slot"
        and c.name in {"sot_customer_time", "sot_commit_timing"}
        and str(c.value or "").strip()
        and str(c.value).strip().lower() not in {"unwilling", "no", "none", "unknown"}
        for c in commands
    )
    # Timing slot names stay SOT-shaped for now; PaisaLo will extend via profile later.
    if not supplied_time and profile.flow_prefix != "sot_":
        timing_slots = {
            f"{profile.flow_prefix}customer_time",
            f"{profile.flow_prefix}commit_timing",
        }
        supplied_time = any(
            c.command == "set_slot"
            and c.name in timing_slots
            and str(c.value or "").strip()
            and str(c.value).strip().lower() not in {"unwilling", "no", "none", "unknown"}
            for c in commands
        )
    if supplied_time:
        return commands, False
    target = profile.reversal_target_flow
    if not target:
        return commands, False
    return [Command(command="start_flow", flow=target)], True


def coerce_confirm(
    commands: list[Command],
    awaiting_slot: str,
    transcript: str,
    *,
    profile: TenantRuntimeProfile,
) -> list[Command]:
    slot = profile.final_confirm_slot
    if not slot or awaiting_slot != slot:
        return commands
    if any(c.command == "set_slot" and c.name == slot for c in commands):
        return commands
    timing_slots = {"sot_customer_time", "sot_commit_timing"}
    if profile.flow_prefix != "sot_":
        timing_slots = {
            f"{profile.flow_prefix}customer_time",
            f"{profile.flow_prefix}commit_timing",
        }
    restated = any(
        c.command == "set_slot" and c.name in timing_slots for c in commands
    )
    if not restated:
        return commands
    low = (transcript or "").lower()
    value = "no" if any(cue in low for cue in profile.cues("negation")) else "yes"
    return [Command(command="set_slot", name=slot, value=value)]


def coerce_link_received(
    commands: list[Command],
    awaiting_slot: str,
    transcript: str,
    *,
    profile: TenantRuntimeProfile,
) -> list[Command]:
    slot = profile.link_received_slot
    if not slot or awaiting_slot != slot:
        return commands
    low = (transcript or "").strip().lower()
    value = (
        "not_received"
        if any(c in low for c in profile.cues("link_not_received"))
        else "received"
    )
    commands = [
        c for c in commands if not (c.command == "set_slot" and c.name == slot)
    ]
    return [*commands, Command(command="set_slot", name=slot, value=value)]


_REASON_CATCHALL_MAX = 140


def _extract_committed_date(transcript: str, *, today: date | None = None) -> str | None:
    """G-B4-02: extract a borrower-committed date from a free-text timeline.

    Returns an ISO ``YYYY-MM-DD`` string, or ``None``. Handles ISO dates,
    ``dd/mm/yyyy`` / ``dd-mm-yyyy``, English month names, and Devanagari month
    names. Year defaults to the current year when omitted.
    """
    if not transcript:
        return None
    today = today or date.today()
    text = transcript

    # ISO yyyy-mm-dd
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except ValueError:
            pass

    # dd/mm/yyyy or dd-mm-yyyy
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", text)
    if m:
        d, mo, y = int(m[1]), int(m[2]), int(m[3])
        if y < 100:
            y += 2000
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            pass

    # English month names (full + abbreviations).
    en_months = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
        "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
        "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    # Devanagari month names.
    hi_months = {
        "जनवरी": 1, "फरवरी": 2, "मार्च": 3, "अप्रैल": 4, "मई": 5, "जून": 6,
        "जुलाई": 7, "अगस्त": 8, "अगस्ट": 8, "सितंबर": 9, "सितम्बर": 9, "अक्टूबर": 10,
        "अक्टूबर": 10, "नवंबर": 11, "नवम्बर": 11, "दिसंबर": 12, "दिसम्बर": 12,
    }
    low = text.lower()
    month_num = 0
    for name, num in {**en_months, **hi_months}.items():
        if name in low or name in text:
            month_num = num
            break
    if month_num:
        m = re.search(r"\b(\d{1,2})\b", text)
        if m:
            d = int(m[1])
            ym = re.search(r"\b(\d{4})\b", text)
            y = int(ym[1]) if ym else today.year
            try:
                return date(y, month_num, d).isoformat()
            except ValueError:
                pass
    return None


def coerce_committed_date(
    commands: list[Command],
    awaiting_slot: str,
    transcript: str,
    *,
    profile: TenantRuntimeProfile,
) -> tuple[list[Command], bool]:
    """G-B4-02: write committed_date back when the borrower names a date.

    Fires when awaiting ``profile.reason_slot`` (PaisaLo timeline) and the
    transcript contains a parseable date. Sets ``committed_date`` (ISO) and
    routes the timeline to ``specific_date`` so the assurance-date reply can
    speak the committed date. Runs before ``coerce_reason_catchall`` so the
    raw-text catchall does not swallow a real date.
    """
    slot = (profile.reason_slot or "").strip()
    if not slot or awaiting_slot != slot:
        return commands, False
    if transcript_blank(transcript):
        return commands, False
    iso = _extract_committed_date(transcript)
    if not iso:
        return commands, False
    kept = [
        c
        for c in commands
        if not (c.command == "set_slot" and c.name in {slot, "committed_date"})
        and c.command != "clarify"
    ]
    kept.append(Command(command="set_slot", name="committed_date", value=iso))
    kept.append(Command(command="set_slot", name=slot, value="specific_date"))
    return kept, True


def coerce_reason_catchall(
    commands: list[Command],
    awaiting_slot: str,
    transcript: str,
    *,
    profile: TenantRuntimeProfile,
) -> tuple[list[Command], bool]:
    """LAST-chain free-text reason fill when LLM omitted a valid set_slot.

    Fires only when awaiting ``profile.reason_slot``, transcript is non-blank,
    and commands contain no set_slot for that slot (empty/rejected LLM path).
    """
    slot = (profile.reason_slot or "").strip()
    if not slot or awaiting_slot != slot:
        return commands, False
    if transcript_blank(transcript):
        return commands, False
    for c in commands:
        if (
            c.command == "set_slot"
            and c.name == slot
            and (c.value or "").strip()
        ):
            return commands, False
    value = (transcript or "").strip()[:_REASON_CATCHALL_MAX]
    kept = [
        c
        for c in commands
        if not (c.command == "set_slot" and c.name == slot) and c.command != "clarify"
    ]
    kept.append(Command(command="set_slot", name=slot, value=value))
    return kept, True


def run_coercion_chain(
    commands: list[Command],
    awaiting_slot: str,
    transcript: str,
    *,
    profile: TenantRuntimeProfile,
    on_rails: bool,
    blank_transcript: bool,
) -> tuple[list[Command], dict[str, str | None]]:
    """Execute the scripted coercion chain with existing short-circuit semantics.

    Order (documented in ``profile.coercion_chain``):
    dispute → willing → refusal → {identity, reversal, [confirm], link}
    → reason_catchall (LAST; only when no earlier short-circuit fired).
    Link still runs when reversal fires; identity never short-circuits siblings.

    Returns ``(commands, meta)`` where meta may include ``refusal_matched_via``.
    """
    meta: dict[str, str | None] = {"refusal_matched_via": None}
    if blank_transcript:
        commands = sanitize_blank_transcript_commands(commands)

    commands, dispute_fired = coerce_dispute(
        commands, transcript, on_rails=on_rails, profile=profile
    )
    callback_fired = False
    if not dispute_fired:
        commands, callback_fired = coerce_callback_request(
            commands, transcript, on_rails=on_rails, profile=profile
        )
    willing_fired = False
    refusal_fired = False
    if not dispute_fired and not callback_fired:
        commands, willing_fired = coerce_push_willing(
            commands, awaiting_slot, transcript, profile=profile
        )
    if not dispute_fired and not callback_fired and not willing_fired:
        commands, refusal_fired, refusal_via = coerce_payment_refusal(
            commands, awaiting_slot, transcript, profile=profile
        )
        if refusal_fired:
            meta["refusal_matched_via"] = refusal_via
    if not dispute_fired and not willing_fired and not refusal_fired:
        commands = coerce_identity(
            commands, awaiting_slot, transcript, profile=profile
        )
        commands, reversal_fired = coerce_commit_reversal(
            commands, awaiting_slot, transcript, profile=profile
        )
        if not reversal_fired:
            commands = coerce_confirm(
                commands, awaiting_slot, transcript, profile=profile
            )
        commands = coerce_link_received(
            commands, awaiting_slot, transcript, profile=profile
        )
        # G-B4-02: capture committed_date BEFORE the raw-text catchall swallows
        # a real date; routes the timeline to specific_date so the assurance-date
        # reply can speak the committed date.
        commands, date_fired = coerce_committed_date(
            commands, awaiting_slot, transcript, profile=profile
        )
        # LAST: free-text reason catchall (SOT payment_problem / PaisaLo timeline).
        if not date_fired:
            commands, _ = coerce_reason_catchall(
                commands, awaiting_slot, transcript, profile=profile
            )
    return commands, meta
