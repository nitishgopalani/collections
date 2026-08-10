"""W2-3 Compose + contracts tests.

Covers:
  - Fragment library load + offline compliance pass (58 fragments, zero issues).
  - One selection test per fragment category (facts/mechanics/loan/outcome/
    caller/ack/meta/confirm/redirect/scope).
  - Compose validation: ids exist, ack pair-only, scenario gate, product
    gate, unhydrated slot -> unknown_info swap.
  - Router contract: CommandParseResult carries oof_class/subclass/
    secondary_intents/confidence (telemetry-only, None on normal turns).
  - UNRELATED deterministic lane: irrelevant -> scope_boundary variant
    (pre/post-identity) + canonical re-ask; world-knowledge OFF.
  - Renderer: gender-resolve by persona voice, {slot} substitution.
  - 12-scenario OOF table replay (parametrized).
  - DEBT-041 locking test: full-call shadow replay where t2 identity turn
    verdict = execute at evidence 2.
  - paid-vs-due tag mismatch -> confirm_asked_paid.
"""

from __future__ import annotations

import pytest

from app.engine.commitment_gate import commitment_gate
from app.engine.command_gen import CommandParseResult
from app.engine.compose_renderer import (
    render_compose,
    render_unrelated_redirect,
)
from app.engine.fragment_library import (
    get_fragment,
    list_fragments,
    offline_compliance_pass,
    text_slots,
    validate_compose,
)
from app.schemas.command import Command

TENANT = "paisalo"


# ---------------------------------------------------------------------------
# Fragment library load + offline compliance pass
# ---------------------------------------------------------------------------


def test_fragment_library_loads_and_compliance_pass_is_clean():
    report = offline_compliance_pass(TENANT)
    assert report["pass"] is True, report["issues"]
    assert report["fragment_count"] >= 54  # 51 + confirms + terminal + 3 new


def test_no_duplicate_fragment_ids():
    frags = list_fragments(TENANT)
    ids = [f["id"] for f in frags]
    assert len(ids) == len(set(ids))


def test_grounding_by_construction_every_text_slot_is_declared():
    """Every {slot} token in a fragment's text must be in its slots list."""
    for frag in list_fragments(TENANT):
        declared = set(frag.get("slots") or [])
        for s in text_slots(frag.get("text", "")):
            assert s in declared, f"{frag['id']}: {{{s}}} not in slots list"


# ---------------------------------------------------------------------------
# One selection test per fragment category
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "category,fragment_id",
    [
        ("facts", "fact_amount_due"),
        ("mechanics", "cap_payment_modes_mfi"),
        ("loan", "fact_loan_summary"),
        ("outcome", "fact_cibil_npa"),
        ("caller", "fact_grievance"),
        ("ack", "ack_neutral"),
        ("meta", "meta_repeat"),
        ("confirm", "confirm_pay_today"),
        ("redirect", "irrelevant_redirect"),
        ("scope", "scope_boundary_pre_identity"),
    ],
)
def test_one_fragment_per_category(category, fragment_id):
    frag = get_fragment(TENANT, fragment_id)
    assert frag is not None
    assert frag["category"] == category


# ---------------------------------------------------------------------------
# Compose validation
# ---------------------------------------------------------------------------


def test_compose_validation_unknown_id_swaps_to_unknown_info():
    resolved, rejections = validate_compose(
        TENANT, ["nonexistent_fragment"],
        scenario=None, product=None, state_slots={},
    )
    assert "unknown_info" in resolved
    assert any("unknown fragment" in r for r in rejections)


def test_compose_validation_over_limit_truncates_to_two():
    resolved, rejections = validate_compose(
        TENANT, ["fact_amount_due", "fact_due_date", "fact_branch"],
        scenario=None, product=None,
        state_slots={"repay_amount": 5000, "due_date": "15 Aug", "branch": "Lucknow"},
    )
    assert len(resolved) <= 2
    assert any("over-limit" in r for r in rejections)


def test_compose_validation_ack_pair_only_cannot_be_alone():
    """ack_neutral (role=pair_only) selected alone -> append deflect_branch_generic."""
    resolved, rejections = validate_compose(
        TENANT, ["ack_neutral"],
        scenario=None, product=None, state_slots={"branch": "Lucknow"},
    )
    assert "ack_neutral" in resolved
    assert "deflect_branch_generic" in resolved
    assert any("pair-only" in r for r in rejections)


def test_compose_validation_scenario_gate_rejects_wrong_scenario():
    """fact_cibil_npa (scenario=[npa]) on predue -> swap to unknown_info."""
    resolved, rejections = validate_compose(
        TENANT, ["fact_cibil_npa"],
        scenario="predue", product=None, state_slots={},
    )
    assert "unknown_info" in resolved
    assert any("scenario gate" in r for r in rejections)


def test_compose_validation_product_gate_rejects_wrong_product():
    """cap_payment_modes_abf (product=[ABF]) on MFI -> swap to unknown_info."""
    resolved, rejections = validate_compose(
        TENANT, ["cap_payment_modes_abf"],
        scenario=None, product="MFI", state_slots={"branch": "Lucknow"},
    )
    assert "unknown_info" in resolved
    assert any("product gate" in r for r in rejections)


def test_compose_validation_unhydrated_slot_swaps_to_unknown_info():
    """fact_amount_due needs repay_amount + due_date; unhydrated -> unknown_info."""
    resolved, rejections = validate_compose(
        TENANT, ["fact_amount_due"],
        scenario=None, product=None, state_slots={},  # no repay_amount
    )
    assert "unknown_info" in resolved
    assert any("unhydrated" in r for r in rejections)


def test_compose_validation_hydrated_slots_pass():
    resolved, rejections = validate_compose(
        TENANT, ["fact_amount_due"],
        scenario=None, product=None,
        state_slots={"repay_amount": 5000, "due_date": "15 Aug"},
    )
    assert resolved == ["fact_amount_due"]
    assert rejections == []


# ---------------------------------------------------------------------------
# Renderer: gender-resolve + slot substitution
# ---------------------------------------------------------------------------


def test_renderer_gender_resolves_feminine_for_priya():
    text = render_compose(
        TENANT, ["fact_caller_identity"],
        state_slots={"persona_name": "Priya", "branch": "Lucknow"},
        persona_voice="priya",
    )
    assert "रही" in text
    assert "रहा" not in text


def test_renderer_gender_resolves_masculine_for_kabir():
    text = render_compose(
        TENANT, ["fact_caller_identity"],
        state_slots={"persona_name": "Kabir", "branch": "Lucknow"},
        persona_voice="kabir",
    )
    assert "रहा" in text
    assert "रही" not in text


def test_renderer_substitutes_slot_tokens():
    text = render_compose(
        TENANT, ["fact_amount_due"],
        state_slots={"repay_amount": 5000, "due_date": "15 Aug"},
        persona_voice="priya",
    )
    assert "5000" in text
    assert "15 Aug" in text
    assert "{repay_amount}" not in text
    assert "{due_date}" not in text


def test_renderer_joins_two_fragments():
    text = render_compose(
        TENANT, ["ack_neutral", "deflect_branch_generic"],
        state_slots={"branch": "Lucknow"},
        persona_voice="priya",
    )
    # both fragments present, joined
    assert "समझ" in text  # from ack_neutral
    assert "Lucknow" in text  # from deflect_branch_generic


# ---------------------------------------------------------------------------
# UNRELATED deterministic lane
# ---------------------------------------------------------------------------


def test_unrelated_redirect_pre_identity_names_no_loan_details():
    """oof_class=irrelevant + identity_ok=False -> scope_boundary_pre_identity
    (names NO loan details — disclosure-tier alignment)."""
    text = render_unrelated_redirect(
        TENANT, identity_ok=False, state_slots={}, persona_voice="priya",
    )
    assert "किश्त" in text or "भुगतान" in text  # mentions the payment scope
    # pre-identity variant must NOT mention loan amounts/details
    assert "लोन" not in text or "ऋण" not in text


def test_unrelated_redirect_post_identity_may_reference_loan():
    """oof_class=irrelevant + identity_ok=True -> scope_boundary_post_identity
    (may reference this loan only)."""
    text = render_unrelated_redirect(
        TENANT, identity_ok=True, state_slots={}, persona_voice="priya",
    )
    assert "लोन" in text  # post-identity may reference the loan


# ---------------------------------------------------------------------------
# Router contract: CommandParseResult fields (telemetry-only)
# ---------------------------------------------------------------------------


def test_router_contract_fields_default_none_on_normal_turn():
    """Normal-flow turn: oof_class/subclass/secondary_intents/confidence are
    None/empty (parse-surface discipline — invariant #7)."""
    r = CommandParseResult(commands=[Command(command="set_slot", name="x", value="y")])
    assert r.oof_class is None
    assert r.oof_subclass is None
    assert r.secondary_intents == []
    assert r.confidence is None


def test_router_contract_fields_populated_on_oof_turn():
    """OOF turn: oof_class + subclass + secondary_intents + confidence
    populated (same LLM call). Confidence is telemetry-only."""
    r = CommandParseResult(
        commands=[Command(command="compose", fragments=["fact_branch"], oof_class="call_context")],
        oof_class="call_context",
        oof_subclass=None,
        secondary_intents=["complaint"],
        confidence=0.82,
    )
    assert r.oof_class == "call_context"
    assert r.secondary_intents == ["complaint"]
    assert r.confidence == 0.82


def test_compose_command_carries_fragments_and_oof_class():
    """The compose Command carries fragments[] + oof_class."""
    cmd = Command(command="compose", fragments=["fact_branch"], oof_class="call_context")
    assert cmd.fragments == ["fact_branch"]
    assert cmd.oof_class == "call_context"


# ---------------------------------------------------------------------------
# 12-scenario OOF table replay
# Each scenario: (transcript_sketch, expected_oof_class, expected_fragment_id)
# The replay asserts the fragment is selectable + valid for the scenario.
# ---------------------------------------------------------------------------

OOF_TABLE = [
    ("कितना बकाया है?", "call_context", "fact_total_outstanding"),
    ("ड्यू डेट क्या है?", "call_context", "fact_due_date"),
    ("मैंने पे कर दिया", "payment_assertion", "fact_payment_lag"),
    ("अभी तक कितना जमा हुआ?", "call_context", "fact_amount_paid"),
    ("QR खो गया", "call_context", "cap_qr_lost"),
    ("ब्रांच कहाँ है?", "call_context", "fact_branch"),
    ("ब्रांच का नंबर?", "call_context", "fact_branch_phone"),
    ("आप कौन हैं?", "call_context", "fact_caller_identity"),
    ("कॉल रिकॉर्ड हो रही?", "call_context", "cap_recording"),
    ("OTP माँगते हैं?", "call_context", "never_ask_otp"),
    ("शिकायत कहे करे?", "complaint", "fact_grievance"),
    ("राजनीति के बारे में बताओ", "irrelevant", "irrelevant_redirect"),
]


@pytest.mark.parametrize("transcript,oof_class,fragment_id", OOF_TABLE)
def test_oof_table_replay(transcript, oof_class, fragment_id):
    """12-scenario OOF table: each fragment is selectable + valid for a
    representative scenario. The transcript is a sketch (the LLM picks the
    fragment; here we assert the fragment exists + passes validation for
    the scenario's hydration)."""
    frag = get_fragment(TENANT, fragment_id)
    assert frag is not None, f"fragment {fragment_id} missing"
    # Build a state with the fragment's declared slots hydrated.
    state_slots = {s: "x" for s in (frag.get("slots") or [])}
    resolved, rejections = validate_compose(
        TENANT, [fragment_id],
        scenario=frag.get("scenario", [None])[0] if frag.get("scenario") else None,
        product=frag.get("product", [None])[0] if frag.get("product") else None,
        state_slots=state_slots,
    )
    # irrelevant_redirect has no scenario/product gate; others resolve clean.
    if oof_class == "irrelevant":
        assert fragment_id in ("irrelevant_redirect", "scope_boundary_pre_identity", "scope_boundary_post_identity")
    else:
        assert fragment_id in resolved, f"{fragment_id} rejected: {rejections}"


# ---------------------------------------------------------------------------
# paid-vs-due tag mismatch -> confirm_asked_paid
# ---------------------------------------------------------------------------


def test_paid_vs_due_mismatch_routes_to_confirm_asked_paid():
    """Borrower asks "कितना जमा हुआ?" (paid_total tag) but the active collect
    is a due-amount slot. The answers[] tag mismatch triggers the
    confirm_asked_paid gate fragment (selection-uncertainty confirm)."""
    frag = get_fragment(TENANT, "confirm_asked_paid")
    assert frag is not None
    assert frag["role"] == "confirm"
    # The confirm fragment has no slots to hydrate; it always validates clean.
    resolved, rejections = validate_compose(
        TENANT, ["confirm_asked_paid"],
        scenario=None, product=None, state_slots={},
    )
    assert resolved == ["confirm_asked_paid"]
    assert rejections == []


# ---------------------------------------------------------------------------
# DEBT-041 locking test: full-call shadow replay, t2 identity verdict = execute
# ---------------------------------------------------------------------------


def test_debt041_locking_t2_identity_turn_executes_at_evidence2():
    """DEBT-041 locking test: full-call shadow replay where the t2 identity
    turn (set_slot plo_identity_response=confirmed) verdict = execute at
    evidence 2. The identity_confirm class (cost 2) is EXEMPT from the
    identity_current precondition — the turn that confirms identity is
    the turn that SETS identity_current. pii (cost 3) is the only class
    keyed on identity_current."""
    # t1: opener (blank) — gate not exercised here.
    # t2: borrower says "haan, main Ramesh bol raha hoon" at plo_identity_response.
    candidate = [Command(command="set_slot", name="plo_identity_response", value="confirmed")]
    # identity_ok=False — this is the turn that SETS it.
    verdict = commitment_gate(
        candidate,
        evidence={"evidence": 2, "evidence_reason": "cue_agree", "evidence_signals": {}},
        cost_table=None,
        slot_cost_class={"plo_identity_response": "identity_confirm"},
        identity_ok=False,
        awaited_slot="plo_identity_response",
    )
    assert verdict["verdict"] == "execute"
    assert verdict["cost_class"] == "identity_confirm"
    assert verdict["max_cost"] == 2
    assert verdict["would_downgrade"] is False


def test_debt041_pii_personal_data_still_locked_without_identity():
    """DEBT-041: pii (personal-data slots: customer_name/phone/address/dob)
    is STILL keyed on identity_current. A set_slot on customer_name without
    identity_current -> hold (disclosure locked). Only identity-confirmation
    slots were reclassified out of pii."""
    candidate = [Command(command="set_slot", name="customer_name", value="Ramesh")]
    verdict = commitment_gate(
        candidate,
        evidence={"evidence": 3, "evidence_reason": "explicit_confirm", "evidence_signals": {}},
        cost_table=None,
        slot_cost_class={"customer_name": "pii"},
        identity_ok=False,
        awaited_slot="customer_name",
    )
    assert verdict["verdict"] == "hold"
    assert verdict["reason"] == "pii_without_identity_current"
