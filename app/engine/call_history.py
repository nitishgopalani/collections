"""W3-2 call-history + mid-call memory — sessions-store hydration (R1).

Prior-call fields live on the session record we already persist. A compact
borrower-scoped index (same store, not a new table) lets a new ``call_id``
read ``attempts_today`` / ``last_*`` from earlier sessions.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.schemas.flow import FlowSet
from app.schemas.state import ConversationState, Frame

_IST = ZoneInfo("Asia/Kolkata")

_SCENARIO_FLOW = {
    "predue": "plo_predue",
    "ondue": "plo_ondue",
    "postdue1": "plo_postdue1",
    "postdue2": "plo_postdue2",
    "postdue3": "plo_postdue3",
    "npa": "plo_npa",
}

_SCENARIO_VOICE = {
    "predue": "simran",
    "ondue": "simran",
    "postdue1": "neha",
    "postdue2": "neha",
    "postdue3": "kabir",
    "npa": "amit",
}

_SCENARIO_PERSONA = {
    "predue": "अंजली",
    "ondue": "अंजली",
    "postdue1": "नेहा",
    "postdue2": "नेहा",
    "postdue3": "अर्जुन",
    "npa": "अमन",
}

_KNOWN_SCENARIOS = frozenset(_SCENARIO_FLOW)


def sessions_key(borrower_id: str) -> str:
    return f"sessions:{borrower_id}"


def reference_now(today: date) -> datetime:
    """Noon IST on the pinned call date — deterministic 24h window for tests."""
    return datetime(today.year, today.month, today.day, 12, 0, tzinfo=_IST)


def parse_ts(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        ts = raw
        return ts if ts.tzinfo else ts.replace(tzinfo=_IST)
    text = str(raw).strip()
    if not text:
        return None
    try:
        ts = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.combine(date.fromisoformat(text[:10]), datetime.min.time(), _IST)
        except ValueError:
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_IST)
    return ts


def parse_iso_date(raw: Any) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    text = str(raw).strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def is_repeat_call(last_call_ts: Any, today: date) -> bool:
    ts = parse_ts(last_call_ts)
    if ts is None:
        return False
    return (reference_now(today) - ts) <= timedelta(hours=24)


def should_honour_ptp(last_ptp_date: Any, today: date) -> bool:
    promised = parse_iso_date(last_ptp_date)
    return promised is not None and promised > today


def resolve_plo_scenario(slots: dict[str, Any]) -> str:
    override = str(
        slots.get("plo_scenario") or slots.get("plo_scenario_override") or ""
    ).strip().lower()
    if override in _KNOWN_SCENARIOS:
        return override
    npa_raw = slots.get("npa_flag")
    npa = bool(npa_raw) and str(npa_raw).lower() not in {"0", "false", "no", ""}
    if npa:
        return "npa"
    try:
        dpd = int(
            slots.get("days_past_due")
            if slots.get("days_past_due") is not None
            else slots.get("dpd")
            or 0
        )
    except (TypeError, ValueError):
        dpd = 0
    if dpd < 0:
        return "predue"
    if dpd == 0:
        return "ondue"
    if dpd <= 30:
        return "postdue1"
    if dpd <= 60:
        return "postdue2"
    return "postdue3"


def apply_scenario_defaults(slots: dict[str, Any], scenario: str) -> None:
    slots["plo_scenario"] = scenario
    slots.setdefault("voice_id", _SCENARIO_VOICE.get(scenario, "neha"))
    slots.setdefault("persona_name", _SCENARIO_PERSONA.get(scenario, "अंजली"))
    slots.setdefault("tts_model", "bulbul:v3")
    pace = {"postdue3": 0.95, "npa": 1.0}.get(scenario, 1.1)
    if slots.get("tts_pace") is None:
        slots["tts_pace"] = pace


def pending_collect(scenario: str, flows: FlowSet) -> tuple[str, int, str]:
    """Return (flow_name, step_index, collect_slot) for the pending re-ask."""
    flow_name = _SCENARIO_FLOW.get(scenario, "plo_postdue3")
    flow = flows.flows.get(flow_name)
    default_slot = "plo_timeline" if scenario == "npa" else "plo_payment_intent"
    if flow is None:
        return flow_name, 0, default_slot
    prefer_ids = {"wait_intent", "wait_timeline"}
    prefer_slots = {"plo_payment_intent", "plo_timeline"}
    for i, step in enumerate(flow.steps):
        if step.id in prefer_ids or (step.collect and step.collect in prefer_slots):
            return flow_name, i, step.collect or default_slot
    for i, step in enumerate(flow.steps):
        if step.collect:
            return flow_name, i, step.collect
    return flow_name, 0, default_slot


def land_at_pending_collect(
    state: ConversationState,
    flows: FlowSet,
    *,
    scenario: str,
) -> ConversationState:
    """Skip opener + greet_detail; park the flow on the pending collect step."""
    updated = state.model_copy(deep=True)
    slots = dict(updated.slots)
    apply_scenario_defaults(slots, scenario)
    slots["identity_ok"] = True
    slots["plo_identity_response"] = "confirmed"
    flow_name, step_index, collect_slot = pending_collect(scenario, flows)
    slots["last_question_slot"] = collect_slot
    updated.slots = slots
    updated.flow_stack = [Frame(flow=flow_name, step_index=step_index)]
    return updated


def _sorted_prior(records: list[dict[str, Any]], current_call_id: str) -> list[dict[str, Any]]:
    prior = [r for r in records if str(r.get("call_id") or "") != current_call_id]
    def _key(rec: dict[str, Any]) -> datetime:
        return parse_ts(rec.get("ts") or rec.get("last_call_ts")) or datetime.min.replace(tzinfo=_IST)
    return sorted(prior, key=_key, reverse=True)


def hydrate_call_history(
    state: ConversationState,
    records: list[dict[str, Any]] | None,
    today: date,
) -> ConversationState:
    """Fill attempts_today / last_* from prior session records (R1)."""
    updated = state.model_copy(deep=True)
    slots = dict(updated.slots)
    prior = _sorted_prior(list(records or []), state.call_id)
    same_day = 0
    for rec in prior:
        ts = parse_ts(rec.get("ts") or rec.get("last_call_ts"))
        if ts is not None and ts.astimezone(_IST).date() == today:
            same_day += 1
    slots["attempts_today"] = same_day + 1
    last = prior[0] if prior else None
    if last is not None:
        slots["last_disposition"] = last.get("disposition")
        slots["last_call_ts"] = last.get("ts") or last.get("last_call_ts")
        last_ptp = last.get("ptp_date") or last.get("last_ptp_date")
        if not last_ptp:
            for rec in prior:
                cand = rec.get("ptp_date") or rec.get("last_ptp_date")
                if cand:
                    last_ptp = cand
                    break
        if last_ptp:
            slots["last_ptp_date"] = last_ptp
    else:
        slots.setdefault("last_disposition", None)
        slots.setdefault("last_call_ts", None)
        slots.setdefault("last_ptp_date", None)
    slots["repeat_call"] = is_repeat_call(slots.get("last_call_ts"), today)
    slots["ptp_honour"] = should_honour_ptp(slots.get("last_ptp_date"), today)
    if not slots.get("_session_ts"):
        slots["_session_ts"] = reference_now(today).isoformat()
    updated.slots = slots
    return updated


def build_session_record(state: ConversationState) -> dict[str, Any]:
    """Compact session row written back to the same store (R1)."""
    slots = state.slots
    ptp = slots.get("ptp_date") or slots.get("committed_date")
    return {
        "call_id": state.call_id,
        "ts": slots.get("_session_ts") or slots.get("last_call_ts"),
        "disposition": slots.get("disposition"),
        "ptp_date": ptp,
        "last_ptp_date": slots.get("last_ptp_date"),
        "last_disposition": slots.get("last_disposition"),
        "last_call_ts": slots.get("last_call_ts"),
        "attempts_today": slots.get("attempts_today"),
        "tenant_id": state.tenant_id,
    }


def detect_payment_claim(transcript: str, cues: tuple[str, ...]) -> bool:
    if not transcript or not cues:
        return False
    lowered = transcript.casefold()
    return any(cue.casefold() in lowered for cue in cues if cue)


def tools_are_live(tools: Any) -> bool:
    """True when tools may refetch borrower-state (live HTTP or stub DB).

    Simulate stays false — fixtures must not be treated as a live LMS.
    """
    mode = getattr(tools, "mode", None)
    if mode in {"live", "stub"}:
        return True
    return type(tools).__name__ == "LiveToolClient"
