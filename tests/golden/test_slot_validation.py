"""Declarative slot validation (F4) + ambiguity clarification (F6) + clarify re-ask (F5)."""

from app.config import tenant_config
from app.engine.nlg import draft_reply_resolved
from app.engine.slot_validation import (
    FACT_SLOTS,
    is_valid_clock_time,
    validate_commands,
)
from app.engine.tracker import new_conversation_state
from app.engine.turn import _clarify_if_ambiguous
from app.flows.loader import load_all_flows
from app.schemas.command import Command

FLOWS = load_all_flows()


def _state(**slots):
    state = new_conversation_state("call-v", "salary_on_time", "b-v")
    state.slots.update(slots)
    return state


# ---------------------------------------------------------------------------
# F4a — fact slots are read-only for the LLM
# ---------------------------------------------------------------------------


def test_blocks_due_date_overwrite():
    kept, dropped = validate_commands(
        [Command(command="set_slot", name="due_date", value="2026-07-01")]
    )
    assert kept == []
    assert dropped and "due_date" in dropped[0]


def test_blocks_all_fact_slots():
    for slot in FACT_SLOTS:
        kept, _ = validate_commands(
            [Command(command="set_slot", name=slot, value="123")]
        )
        assert kept == [], f"{slot} should be blocked"


def test_keeps_normal_collect_slot():
    cmds = [Command(command="set_slot", name="sot_payment_intent", value="willing")]
    kept, dropped = validate_commands(cmds)
    assert kept == cmds
    assert dropped == []


def test_passes_through_non_set_slot():
    cmds = [Command(command="start_flow", flow="sot_obj_busy"), Command(command="clarify")]
    kept, dropped = validate_commands(cmds)
    assert kept == cmds
    assert dropped == []


# ---------------------------------------------------------------------------
# F4c — typed validation: sot_customer_time must be a clock time
# ---------------------------------------------------------------------------


def test_customer_time_accepts_clock_times():
    for good in ("shaam 6 baje", "6:30", "raat tak", "dopahar 2 baje", "शाम ५ बजे"):
        assert is_valid_clock_time(good) is True, good


def test_customer_time_rejects_day_words_and_iso():
    for bad in ("kal", "parso", "due date ko", "2026-07-05", "aaj", ""):
        assert is_valid_clock_time(bad) is False, bad


def test_customer_time_iso_with_time_survives():
    assert is_valid_clock_time("2026-07-05 shaam 6 baje") is True


def test_validate_drops_day_word_for_customer_time():
    kept, dropped = validate_commands(
        [Command(command="set_slot", name="sot_customer_time", value="kal")]
    )
    assert kept == []
    assert dropped and "sot_customer_time" in dropped[0]


def test_validate_keeps_valid_customer_time():
    cmds = [Command(command="set_slot", name="sot_customer_time", value="shaam 6 baje")]
    kept, _ = validate_commands(cmds)
    assert kept == cmds


# ---------------------------------------------------------------------------
# F6 — clarify on ambiguous flow candidates
# ---------------------------------------------------------------------------


def _cands(*pairs):
    return [{"name": n, "score": s} for n, s in pairs]


def test_ambiguous_tie_becomes_clarify():
    cmds = [Command(command="start_flow", flow="sot_obj_busy")]
    out, fired = _clarify_if_ambiguous(
        cmds, _cands(("sot_obj_busy", 0.61), ("sot_obj_hold", 0.60)), delta=0.04
    )
    assert fired is True
    assert len(out) == 1 and out[0].command == "clarify"


def test_clear_winner_is_left_alone():
    cmds = [Command(command="start_flow", flow="sot_obj_busy")]
    out, fired = _clarify_if_ambiguous(
        cmds, _cands(("sot_obj_busy", 0.80), ("sot_obj_hold", 0.40)), delta=0.04
    )
    assert fired is False
    assert out is cmds


def test_ambiguity_ignored_when_set_slot_present():
    cmds = [
        Command(command="start_flow", flow="sot_obj_busy"),
        Command(command="set_slot", name="sot_payment_intent", value="willing"),
    ]
    out, fired = _clarify_if_ambiguous(
        cmds, _cands(("sot_obj_busy", 0.61), ("sot_obj_hold", 0.60)), delta=0.04
    )
    assert fired is False


def test_ambiguity_needs_two_candidates():
    cmds = [Command(command="start_flow", flow="sot_obj_busy")]
    out, fired = _clarify_if_ambiguous(cmds, _cands(("sot_obj_busy", 0.61)), delta=0.04)
    assert fired is False


# ---------------------------------------------------------------------------
# F5 — a clarify while collecting re-asks the CURRENT slot (not a dead-end)
# ---------------------------------------------------------------------------


def test_clarify_reasks_current_slot():
    tenant_cfg = tenant_config("salary_on_time")
    state = _state(last_question_slot="sot_customer_time")
    resolved = draft_reply_resolved(
        reply_id=None,
        question_slot=None,
        commands=[Command(command="clarify")],
        state=state,
        flows=FLOWS,
        tenant_cfg=tenant_cfg,
    )
    # Should re-render the time prompt, not the generic clarify fallback.
    assert resolved.text != tenant_cfg.clarify_reply
    assert resolved.text.strip() != ""
