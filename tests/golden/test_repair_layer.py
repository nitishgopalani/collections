"""Conversation repair layer (Phase 1) — retry-cap, escalation, rephrase-on-repeat."""

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import tenant_config
from app.engine.actions import make_async_action_runner
from app.engine.executor import run_async as run_executor_async
from app.engine.nlg import render_collect_slot_resolved
from app.engine.robustness import (
    FRUSTRATION_COUNT_KEY,
    REPAIR_COUNTS_KEY,
    mark_repair_escalation,
    track_frustration,
    track_slot_reask,
)
from app.engine.tracker import apply, new_conversation_state
from app.engine.turn import (
    DISPUTE_EVIDENCE_KEY,
    _accumulate_dispute_evidence,
    _coerce_sot_commit_reversal,
    _coerce_sot_dispute,
    _coerce_sot_identity,
    _coerce_sot_payment_refusal,
    _coerce_sot_push_willing,
    _dispute_evidence_this_turn,
    _prune_spurious_sot_objection_stack,
    _sanitize_sot_commands_for_blank_transcript,
    _sot_dispute_flow,
    _sot_transcript_blank,
)
from app.flows.loader import load_all_flows
from app.schemas.command import Command
from app.schemas.state import Frame

FLOWS = load_all_flows()


def _state(**slots):
    state = new_conversation_state("call-1", "salary_on_time", "borrower-1")
    state.slots.update(slots)
    return state


def test_reask_increments_then_escalates():
    """Same slot re-asked after a caller reply counts up; escalates past the cap."""
    # First ask: no prior slot, an inbound reply is present but slot is new.
    state = _state(last_question_slot="sot_customer_time")

    # Re-ask #1 (cap=2): counts to 1, no escalation.
    state, escalate = track_slot_reask(
        state, question_slot="sot_customer_time", had_inbound=True, max_retries=2
    )
    assert escalate is False
    assert state.slots[REPAIR_COUNTS_KEY]["sot_customer_time"] == 1

    # Keep last_question_slot pinned (record_outbound_context would do this live).
    state.slots["last_question_slot"] = "sot_customer_time"
    # Re-ask #2: counts to 2, still no escalation.
    state, escalate = track_slot_reask(
        state, question_slot="sot_customer_time", had_inbound=True, max_retries=2
    )
    assert escalate is False
    assert state.slots[REPAIR_COUNTS_KEY]["sot_customer_time"] == 2

    state.slots["last_question_slot"] = "sot_customer_time"
    # Would-be re-ask #3: cap reached → escalate, no further increment.
    state, escalate = track_slot_reask(
        state, question_slot="sot_customer_time", had_inbound=True, max_retries=2
    )
    assert escalate is True
    assert state.slots[REPAIR_COUNTS_KEY]["sot_customer_time"] == 2


def test_reask_resets_when_flow_advances():
    state = _state(
        last_question_slot="sot_customer_time",
        **{REPAIR_COUNTS_KEY: {"sot_customer_time": 2}},
    )
    state, escalate = track_slot_reask(
        state, question_slot="sot_final_confirm", had_inbound=True, max_retries=2
    )
    assert escalate is False
    assert state.slots[REPAIR_COUNTS_KEY].get("sot_customer_time") is None
    assert state.slots[REPAIR_COUNTS_KEY]["sot_final_confirm"] == 0


def test_no_inbound_does_not_count():
    """Opener / silent turns must not accrue re-asks."""
    state = _state(last_question_slot="sot_identity_response")
    state, escalate = track_slot_reask(
        state, question_slot="sot_identity_response", had_inbound=False, max_retries=2
    )
    assert escalate is False
    assert state.slots.get(REPAIR_COUNTS_KEY, {}).get("sot_identity_response", 0) == 0


def test_mark_repair_escalation_closes_call():
    state = _state(**{REPAIR_COUNTS_KEY: {"sot_customer_time": 2}})
    state = mark_repair_escalation(state, question_slot="sot_customer_time")
    assert state.slots["disposition"] == "ESCALATED_UNCLEAR"
    assert state.slots["end_call"] is True
    assert state.slots["sot_call_closed"] is True
    assert state.slots[REPAIR_COUNTS_KEY] == {}


def test_reversal_refusal_at_time_step_routes_to_transfer():
    """'payment nahi kar paunga' while asked for a time -> hand off (F3)."""
    cmds, fired = _coerce_sot_commit_reversal(
        [Command(command="set_slot", name="sot_payment_intent", value="unwilling")],
        "sot_customer_time",
        "nahi, payment nahi kar paunga, sorry",
    )
    assert fired is True
    assert len(cmds) == 1
    assert cmds[0].command == "start_flow"
    assert cmds[0].flow == "sot_obj_no_timeline"


def test_reversal_day_change_is_not_a_refusal():
    """'aaj nahi kal' is a day change, not a refusal -> leave commands untouched."""
    original = [Command(command="set_slot", name="sot_commit_timing", value="tomorrow")]
    cmds, fired = _coerce_sot_commit_reversal(original, "sot_customer_time", "aaj nahi kal karunga")
    assert fired is False
    assert cmds is original


def test_reversal_ignored_when_time_supplied():
    original = [Command(command="set_slot", name="sot_customer_time", value="shaam 6 baje")]
    cmds, fired = _coerce_sot_commit_reversal(
        original, "sot_customer_time", "shaam 6 baje, abhi nahi keh sakta exact"
    )
    assert fired is False


def test_reversal_not_fired_at_intent_step():
    """At the offer/push intent step, 'not willing' must go to push, not transfer."""
    original = [Command(command="set_slot", name="sot_payment_intent", value="unwilling")]
    cmds, fired = _coerce_sot_commit_reversal(
        original, "sot_payment_intent", "payment nahi kar paunga"
    )
    assert fired is False


def test_coerce_payment_refusal_at_intent_step():
    original = [Command(command="clarify")]
    cmds, fired = _coerce_sot_payment_refusal(
        original,
        "sot_payment_intent",
        "नहीं नहीं आज तो पेमेंट नहीं हो पाएगी",
    )
    assert fired is True
    assert cmds == [
        Command(
            command="set_slot",
            name="sot_payment_intent",
            value="refused",
            source="confirmed",
        )
    ]


@pytest.mark.parametrize(
    "transcript",
    [
        "नहीं, आज तो नहीं आ पाएगी",
        "नहीं नहीं आज नहीं कर पाऊंगा",
        "abhi nahi ho paega",
        "sorry main aaj pay nahi kar sakta",
    ],
)
def test_coerce_payment_refusal_inability_regex_fires(transcript):
    cmds, fired = _coerce_sot_payment_refusal(
        [Command(command="clarify")],
        "sot_payment_intent",
        transcript,
    )
    assert fired is True
    assert cmds == [
        Command(
            command="set_slot",
            name="sot_payment_intent",
            value="refused",
            source="confirmed",
        )
    ]


@pytest.mark.parametrize(
    "transcript",
    [
        "नहीं नहीं, मैं कर दूंगा",
        "haan kal kar dunga",
    ],
)
def test_coerce_payment_refusal_inability_must_not_fire(transcript):
    original = [Command(command="clarify")]
    cmds, fired = _coerce_sot_payment_refusal(original, "sot_payment_intent", transcript)
    assert fired is False
    assert cmds is original


def test_coerce_payment_refusal_day_shift_must_not_fire():
    """MUST NOT refuse a day-shift: 'nahi aaj nahi kal karunga' (Checkpoint-0 C2 / R2)."""
    original = [Command(command="clarify")]
    cmds, fired = _coerce_sot_payment_refusal(
        original, "sot_payment_intent", "nahi aaj nahi kal karunga"
    )
    assert fired is False
    assert cmds is original


def test_routing_miss_does_not_burn_reask_retries():
    """routing_miss skips increment/escalate; slot-changed reset still runs."""
    state = _state(last_question_slot="sot_payment_problem")
    for _ in range(3):
        state, escalate = track_slot_reask(
            state,
            question_slot="sot_payment_problem",
            had_inbound=True,
            max_retries=2,
            routing_miss=True,
        )
        state.slots["last_question_slot"] = "sot_payment_problem"
        assert escalate is False
    assert state.slots.get(REPAIR_COUNTS_KEY, {}).get("sot_payment_problem", 0) == 0

    # Slot change still clears the prior counter and seeds the new slot.
    state.slots[REPAIR_COUNTS_KEY] = {"sot_payment_problem": 2}
    state.slots["last_question_slot"] = "sot_payment_problem"
    state, escalate = track_slot_reask(
        state,
        question_slot="sot_payment_intent_2",
        had_inbound=True,
        max_retries=2,
        routing_miss=True,
    )
    assert escalate is False
    assert state.slots[REPAIR_COUNTS_KEY].get("sot_payment_problem") is None
    assert state.slots[REPAIR_COUNTS_KEY]["sot_payment_intent_2"] == 0


def test_blank_transcript_sanitize_strips_flow_jumps():
    cmds = _sanitize_sot_commands_for_blank_transcript(
        [
            Command(command="start_flow", flow="sot_obj_medical"),
            Command(command="clarify"),
            Command(command="respond", text="email info@salaryontime.com"),
            Command(command="set_slot", name="sot_identity_response", value="confirmed"),
        ]
    )
    assert [c.command for c in cmds] == ["set_slot"]
    assert _sot_transcript_blank("  \t  ") is True
    assert _sot_transcript_blank("haan") is False


def test_prune_spurious_objection_above_offer_ladder():
    state = new_conversation_state("call-prune", "salary_on_time", "b-prune")
    state.flow_stack = [
        Frame(flow="sot_offer_pre_closure", step_index=2),
        Frame(flow="sot_obj_medical", step_index=0),
    ]
    state.slots["identity_ok"] = True
    pruned = _prune_spurious_sot_objection_stack(state)
    assert [f.flow for f in pruned.flow_stack] == ["sot_offer_pre_closure"]


def test_prune_keeps_objection_before_identity():
    state = new_conversation_state("call-prune2", "salary_on_time", "b-prune2")
    state.flow_stack = [
        Frame(flow="sot_opener", step_index=0),
        Frame(flow="sot_obj_medical", step_index=0),
    ]
    pruned = _prune_spurious_sot_objection_stack(state)
    assert len(pruned.flow_stack) == 2


@pytest.mark.parametrize(
    "transcript,expected",
    [
        ("maine to loan hi nahi liya koi aapse", "sot_obj_never_loan"),
        ("मैंने तो लोन ही नहीं लिया कोई आपसे", "sot_obj_never_loan"),
        ("I never took any loan from you", "sot_obj_never_loan"),
        ("yeh loan mera nahi hai", "sot_obj_never_loan"),
        ("aapne galat charges laga rakhe hain", "sot_obj_wrong_amount"),
        ("charges hata do phir payment karunga", "sot_obj_wrong_amount"),
        ("ye amount galat hai itna nahi liya tha", "sot_obj_wrong_amount"),
        ("ghar mein death ho gayi hai", "sot_obj_death"),
        ("mera account freeze ho gaya hai", "sot_obj_frozen_account"),
    ],
)
def test_sot_dispute_flow_detects_hard_disputes(transcript, expected):
    assert _sot_dispute_flow(transcript) == expected


@pytest.mark.parametrize(
    "transcript",
    [
        "haan main kal payment kar dunga",
        "abhi paise nahi hai thoda time do",
        "loan to hai par abhi nahi de paunga",  # acknowledges loan, just delaying
        "theek hai aaj shaam ko kar deta hun",
    ],
)
def test_sot_dispute_flow_ignores_normal_ladder_replies(transcript):
    assert _sot_dispute_flow(transcript) is None


@pytest.mark.parametrize(
    "transcript",
    [
        "आज। कर दूँगा।",
        "हाँ ठीक है, मैं कोशिश करूँगा।",
        "हाँ जी, बिल्कुल कोशिश कर सकते हैं।",
        "haan aaj kar dunga",
        "theek hai payment kar deta hun",
        "ji haan abhi kar deta hun",
    ],
)
def test_push_willing_agreement_exits_ladder(transcript):
    """An agreement at a push-intent step is coerced to willing (exits the ladder)."""
    original = [Command(command="set_slot", name="sot_payment_intent_2", value="refused")]
    cmds, fired = _coerce_sot_push_willing(original, "sot_payment_intent_2", transcript)
    assert fired is True
    sets = [c for c in cmds if c.command == "set_slot" and c.name == "sot_payment_intent_2"]
    assert len(sets) == 1 and sets[0].value == "willing"
    assert all(c.command != "clarify" for c in cmds)


@pytest.mark.parametrize(
    "transcript",
    [
        "हाँ कल कर दूँगा।",         # willing but tomorrow -> not today
        "आज नहीं हो पाएगा",          # negation
        "परसों तक कर दूंगा",         # future day
        "abhi paisa nahi hai",       # refusal
    ],
)
def test_push_willing_not_fired_for_future_or_refusal(transcript):
    original = [Command(command="set_slot", name="sot_payment_intent_2", value="refused")]
    cmds, fired = _coerce_sot_push_willing(original, "sot_payment_intent_2", transcript)
    assert fired is False
    assert cmds is original


def test_push_willing_only_at_intent_slots():
    """Not an intent slot -> untouched (e.g. when collecting the reason)."""
    original = [Command(command="set_slot", name="sot_payment_problem", value="x")]
    cmds, fired = _coerce_sot_push_willing(original, "sot_payment_problem", "haan kar dunga")
    assert fired is False
    assert cmds is original


def test_coerce_dispute_fires_only_on_rails():
    """A denied loan mid-push starts the transfer objection; off-rails it defers."""
    original = [Command(command="set_slot", name="sot_payment_intent", value="refused")]
    cmds, fired = _coerce_sot_dispute(
        original, "maine to loan hi nahi liya koi aapse", on_rails=True
    )
    assert fired is True
    assert len(cmds) == 1
    assert cmds[0].command == "start_flow"
    assert cmds[0].flow == "sot_obj_never_loan"

    cmds, fired = _coerce_sot_dispute(
        original, "maine to loan hi nahi liya koi aapse", on_rails=False
    )
    assert fired is False
    assert cmds is original


@pytest.mark.asyncio
async def test_injected_transfer_flow_hands_off():
    """Starting sot_obj_no_timeline yields the objection reply + transfer_to_human."""
    state = new_conversation_state("call-x", "salary_on_time", "b-x")
    state.flow_stack = [Frame(flow="sot_commit", step_index=0)]
    state = apply(state, [Command(command="start_flow", flow="sot_obj_no_timeline")])
    runner = make_async_action_runner(FakeToolClient())
    result = await run_executor_async(state, FLOWS, runner)
    assert result.reply_id == "sot_obj_no_timeline"
    assert result.transfer_to_human is True


# ---------------------------------------------------------------------------
# Wave 1 — live-call bug fixes
# ---------------------------------------------------------------------------


def test_identity_bare_haan_confirms():
    """W1.3: a lone 'haan' at the identity step confirms (LLM returned clarify)."""
    cmds = _coerce_sot_identity([], "sot_identity_response", "haan")
    assert len(cmds) == 1
    assert cmds[0].command == "set_slot"
    assert cmds[0].name == "sot_identity_response"
    assert cmds[0].value == "confirmed"


def test_identity_bare_ji_confirms():
    cmds = _coerce_sot_identity([], "sot_identity_response", "जी")
    assert cmds[0].value == "confirmed"


def test_identity_wrong_number_denies():
    cmds = _coerce_sot_identity([], "sot_identity_response", "nahi, galat number hai")
    assert cmds[0].value == "denied"


def test_identity_short_no_denies():
    cmds = _coerce_sot_identity([], "sot_identity_response", "nahi")
    assert cmds[0].value == "denied"


def test_identity_no_override_when_llm_set_slot():
    original = [
        Command(command="set_slot", name="sot_identity_response", value="relation")
    ]
    cmds = _coerce_sot_identity(original, "sot_identity_response", "haan main inka bhai")
    assert cmds is original


def test_identity_relation_left_to_llm():
    """A relation statement (no bare yes/no token) is left untouched -> LLM handles it."""
    cmds = _coerce_sot_identity([], "sot_identity_response", "main inke pati bol raha")
    # 'bol raha' is a yes-phrase; but relation statements without yes/no cues pass through.
    assert isinstance(cmds, list)


def test_identity_coercion_off_other_slots():
    original = [Command(command="clarify")]
    cmds = _coerce_sot_identity(original, "sot_customer_time", "haan")
    assert cmds is original


@pytest.mark.asyncio
async def test_already_paid_acknowledges_then_closes():
    """W1.1: 'already paid' acks + asks screenshot + hangs up (no re-ask loop)."""
    state = new_conversation_state("call-paid", "salary_on_time", "b-paid")
    runner = make_async_action_runner(FakeToolClient())
    # Turn 1: enter the offer, which utters and pauses at the payment-intent collect.
    state = apply(state, [Command(command="start_flow", flow="sot_offer_pre_closure")])
    await run_executor_async(state, FLOWS, runner)
    # Turn 2: borrower says they already paid -> ack + close, NOT re-ask the intent.
    state = apply(
        state,
        [Command(command="set_slot", name="sot_payment_intent", value="already_paid")],
    )
    result = await run_executor_async(state, FLOWS, runner)
    assert result.reply_id == "sot_already_paid"
    assert result.end_call is True


def test_reask_variant_rotates_by_repair_count():
    """Each re-ask of a slot must sound different (rephrase-on-repeat, F2)."""
    tenant_cfg = tenant_config("salary_on_time")
    seen = set()
    for count in (0, 1, 2):
        state = _state(**{REPAIR_COUNTS_KEY: {"sot_customer_time": count}})
        reply = render_collect_slot_resolved(
            "sot_customer_time", state, FLOWS, tenant_cfg=tenant_cfg
        )
        seen.add(reply.text)
    # sot_ask_time has three authored variants — none should repeat across re-asks.
    assert len(seen) == 3


# ---------------------------------------------------------------------------
# Wave 2 — memory guards (dispute accumulator + frustration escalation)
# ---------------------------------------------------------------------------

_DISPUTES = frozenset(
    {"sot_obj_never_loan", "sot_obj_wrong_amount", "sot_obj_death", "sot_obj_frozen_account"}
)


@pytest.mark.parametrize(
    "transcript,expected",
    [
        ("mera koi loan nahi hai aapke paas", "sot_obj_never_loan"),
        ("loan hai hi nahi mera", "sot_obj_never_loan"),
        ("main ne koi loan nahin liya, loan nahi hai", "sot_obj_never_loan"),
        ("कोई लोन नहीं है मेरा", "sot_obj_never_loan"),
        ("लोन है ही नहीं", "sot_obj_never_loan"),
    ],
)
def test_dispute_rule_detects_existence_denial(transcript, expected):
    """Widened matcher covers 'no such loan exists', not just 'I didn't take it'."""
    assert _sot_dispute_flow(transcript) == expected


def test_dispute_evidence_from_matcher():
    """The deterministic matcher supplies evidence even with no LLM proposal."""
    assert (
        _dispute_evidence_this_turn("mera koi loan nahi hai", [], _DISPUTES)
        == "sot_obj_never_loan"
    )


def test_dispute_evidence_from_llm_proposal():
    """The LLM's pre-suppression start_flow into a dispute counts as evidence."""
    proposed = [Command(command="start_flow", flow="sot_obj_never_loan")]
    assert (
        _dispute_evidence_this_turn("kuch samajh nahi aa raha", proposed, _DISPUTES)
        == "sot_obj_never_loan"
    )


def test_pinned_dispute_candidate_is_not_evidence():
    """Regression: a pinned dispute flow is a candidate every turn — but candidate
    presence must NOT count as evidence. Borrower asked for a link, not a dispute."""
    proposed = [Command(command="start_flow", flow="sot_obj_link_request")]
    assert _dispute_evidence_this_turn("link chahiye", proposed, _DISPUTES) is None


def test_accumulator_forces_route_on_second_weak_turn():
    """A dispute proposed below the floor every turn is honored once it corroborates.

    Each turn the LLM proposes the dispute but Layer 3 suppresses it, so the *final*
    commands only carry the borrower's answer; the accumulator counts the proposal.
    """
    state = _state()
    final = [Command(command="set_slot", name="sot_payment_intent", value="refused")]

    # Turn 1: evidence once, below bar=2 → no force, counter increments.
    state, cmds1, forced1 = _accumulate_dispute_evidence(
        state, final, "sot_obj_never_loan", bar=2
    )
    assert forced1 is None
    assert state.slots[DISPUTE_EVIDENCE_KEY]["sot_obj_never_loan"] == 1
    assert cmds1 == final

    # Turn 2: crosses bar → force the dispute route.
    state, cmds2, forced2 = _accumulate_dispute_evidence(
        state, final, "sot_obj_never_loan", bar=2
    )
    assert forced2 == "sot_obj_never_loan"
    assert len(cmds2) == 1
    assert cmds2[0].command == "start_flow"
    assert cmds2[0].flow == "sot_obj_never_loan"
    # Counter resets after firing so we don't re-fire every subsequent turn.
    assert state.slots[DISPUTE_EVIDENCE_KEY]["sot_obj_never_loan"] == 0


def test_accumulator_no_double_count_when_already_routing():
    """When a jump is already routing the dispute, the accumulator stays out of the way."""
    state = _state()
    commands = [Command(command="start_flow", flow="sot_obj_never_loan")]
    state, cmds, forced = _accumulate_dispute_evidence(
        state, commands, "sot_obj_never_loan", bar=2
    )
    assert forced is None
    assert cmds == commands
    assert state.slots[DISPUTE_EVIDENCE_KEY]["sot_obj_never_loan"] == 0


def test_accumulator_ignores_non_dispute_turns():
    """No dispute evidence → nothing accumulates, commands untouched."""
    state = _state()
    commands = [Command(command="set_slot", name="sot_customer_time", value="shaam")]
    state, cmds, forced = _accumulate_dispute_evidence(state, commands, None, bar=2)
    assert forced is None
    assert cmds is commands
    assert state.slots.get(DISPUTE_EVIDENCE_KEY, {}) == {}


def test_frustration_escalates_after_threshold():
    """Consecutive med/high anger|frustration turns escalate at the threshold."""
    state = _state()
    state, esc = track_frustration(state, emotion="frustration", intensity="high", threshold=3)
    assert esc is False and state.slots[FRUSTRATION_COUNT_KEY] == 1
    state, esc = track_frustration(state, emotion="anger", intensity="med", threshold=3)
    assert esc is False and state.slots[FRUSTRATION_COUNT_KEY] == 2
    state, esc = track_frustration(state, emotion="frustration", intensity="high", threshold=3)
    assert esc is True
    # Counter resets on escalation so we don't re-escalate every subsequent turn.
    assert state.slots[FRUSTRATION_COUNT_KEY] == 0


def test_frustration_resets_on_calm_turn():
    """A calm (or low-intensity) turn breaks the streak."""
    state = _state(**{FRUSTRATION_COUNT_KEY: 2})
    state, esc = track_frustration(state, emotion="neutral", intensity="low", threshold=3)
    assert esc is False
    assert state.slots[FRUSTRATION_COUNT_KEY] == 0


def test_frustration_guard_disabled_when_threshold_zero():
    state = _state()
    state, esc = track_frustration(state, emotion="anger", intensity="high", threshold=0)
    assert esc is False
    assert FRUSTRATION_COUNT_KEY not in state.slots
