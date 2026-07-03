"""Pure-logic unit tests for the Label Transition Layer (LTL).

These exercise app.engine.label_transition directly (no handle_turn), covering
provider selection, detection, decisions, command building, and the shadow/enforce
orchestrator contract.
"""

from types import SimpleNamespace

from app.engine.label_transition import (
    LABEL_STATE_SLOT,
    build_transition_commands,
    detect_current_labels,
    get_label_transition_provider,
    load_label_state,
    run_label_transition,
    transition_allowed,
    update_evidence,
)
from app.schemas.command import Command
from app.schemas.flow import Flow, FlowSet
from app.schemas.label_transition import Decision, Label, LabelStateModel
from app.schemas.state import ConversationState


def _flows(names: list[str]) -> FlowSet:
    return FlowSet(
        flows={n: Flow(description=n, priority="reminder", steps=[]) for n in names}
    )


SOT_FLOWS = _flows(
    [
        "sot_obj_link_request",
        "sot_obj_diff_number_link",
        "sot_obj_no_link_pref",
        "sot_obj_never_loan",
        "sot_obj_wrong_amount",
        "sot_obj_already_paid_q",
        "sot_obj_medical",
        "sot_obj_job_loss",
    ]
)


def _state(**slots) -> ConversationState:
    st = ConversationState(call_id="c1", tenant_id="salary_on_time", borrower_id="b1")
    st.attempts = 1
    st.slots.update(slots)
    return st


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        label_transition_enabled=True,
        label_transition_mode="shadow",
        label_transition_scope="all",
        label_high_risk_block=True,
        label_transition_log_enabled=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _sot_provider():
    return get_label_transition_provider("salary_on_time", None)


# --- Providers -------------------------------------------------------------------
def test_provider_sot_supports_enforce():
    p = get_label_transition_provider("salary_on_time", None)
    assert p.name == "salary_on_time"
    assert p.supports_enforce is True
    assert p.label_to_flow[Label.SUPPORT_PAYMENT_LINK_REQUEST] == "sot_obj_link_request"


def test_provider_generic_shadow_only():
    p = get_label_transition_provider("some_other_tenant", None)
    assert p.name == "generic"
    assert p.supports_enforce is False
    assert p.label_to_flow == {}


# --- Detection -------------------------------------------------------------------
def test_detect_from_start_flow():
    p = _sot_provider()
    cmds = [Command(command="start_flow", flow="sot_obj_never_loan")]
    assert detect_current_labels(cmds, "", "kuch bhi", None, p) == Label.DISPUTE_LOAN_NOT_TAKEN


def test_detect_from_set_slot_refusal():
    p = _sot_provider()
    cmds = [Command(command="set_slot", name="sot_payment_intent", value="refused")]
    assert detect_current_labels(cmds, "", "nahi karunga", None, p) == Label.REFUSAL_HARD


def test_detect_from_transcript_link():
    p = _sot_provider()
    assert (
        detect_current_labels([], "", "sir mujhe payment link bhej do", None, p)
        == Label.SUPPORT_PAYMENT_LINK_REQUEST
    )


def test_detect_loan_not_taken_from_transcript():
    p = _sot_provider()
    assert (
        detect_current_labels([], "", "maine to loan liya hi nahi", None, p)
        == Label.DISPUTE_LOAN_NOT_TAKEN
    )


def test_detect_none_when_no_signal():
    p = _sot_provider()
    assert detect_current_labels([], "", "haan theek hai", None, p) is None


# --- transition_allowed ----------------------------------------------------------
def test_same_label_continues():
    p = _sot_provider()
    ls = LabelStateModel()
    d = transition_allowed(
        p, Label.REFUSAL_HARD, Label.REFUSAL_HARD, _state(), ls, "", True
    )
    assert d == Decision.CONTINUE_CURRENT_FLOW


def test_refusal_then_link_resolves_and_switches():
    p = _sot_provider()
    ls = LabelStateModel()
    d = transition_allowed(
        p, Label.REFUSAL_HARD, Label.SUPPORT_PAYMENT_LINK_REQUEST, _state(), ls,
        "link bhej do", True,
    )
    assert d == Decision.RESOLVE_PREVIOUS_AND_SWITCH


def test_dispute_unresolved_then_link_clarifies():
    p = _sot_provider()
    ls = LabelStateModel()
    ls = update_evidence(ls, Label.DISPUTE_LOAN_NOT_TAKEN, 1)
    d = transition_allowed(
        p, Label.DISPUTE_LOAN_NOT_TAKEN, Label.SUPPORT_PAYMENT_LINK_REQUEST,
        _state(), ls, "link bhej do", True,
    )
    assert d == Decision.CLARIFY_BEFORE_SWITCH


def test_dispute_then_ownership_confirm_then_link_resolves():
    p = _sot_provider()
    ls = LabelStateModel()
    ls = update_evidence(ls, Label.DISPUTE_LOAN_NOT_TAKEN, 1)
    d = transition_allowed(
        p, Label.DISPUTE_LOAN_NOT_TAKEN, Label.SUPPORT_PAYMENT_LINK_REQUEST,
        _state(), ls, "haan ye mera loan hai, link bhej do", True,
    )
    assert d == Decision.RESOLVE_PREVIOUS_AND_SWITCH


def test_wrong_person_unresolved_blocks_money():
    p = _sot_provider()
    ls = LabelStateModel()
    ls = update_evidence(ls, Label.IDENTITY_WRONG_PERSON, 1)
    d = transition_allowed(
        p, Label.IDENTITY_WRONG_PERSON, Label.PAYMENT_WILL_PAY_TODAY,
        _state(), ls, "aaj kar dunga", True,
    )
    assert d == Decision.BLOCK_SWITCH_DUE_TO_HIGH_RISK


def test_legal_threat_keeps_flag_but_allows_payment():
    p = _sot_provider()
    ls = LabelStateModel()
    ls = update_evidence(ls, Label.RISK_LEGAL_THREAT, 1)
    d = transition_allowed(
        p, Label.RISK_LEGAL_THREAT, Label.PAYMENT_WILL_PAY_TODAY,
        _state(), ls, "aaj kar dunga", True,
    )
    assert d == Decision.KEEP_HIGH_RISK_FLAG_BUT_ALLOW_PAYMENT


def test_high_risk_block_disabled_allows_switch():
    p = _sot_provider()
    ls = LabelStateModel()
    ls = update_evidence(ls, Label.DISPUTE_LOAN_NOT_TAKEN, 1)
    d = transition_allowed(
        p, Label.DISPUTE_LOAN_NOT_TAKEN, Label.SUPPORT_PAYMENT_LINK_REQUEST,
        _state(), ls, "link bhej do", False,  # high_risk_block off
    )
    assert d == Decision.SWITCH_FLOW


# --- build_transition_commands ---------------------------------------------------
def test_build_switch_adds_start_flow():
    p = _sot_provider()
    ls = LabelStateModel()
    cmds = [Command(command="set_slot", name="sot_payment_intent", value="willing")]
    out, skip = build_transition_commands(
        Decision.RESOLVE_PREVIOUS_AND_SWITCH, cmds,
        Label.SUPPORT_PAYMENT_LINK_REQUEST, ls, p, SOT_FLOWS,
    )
    assert skip is None
    assert any(c.command == "start_flow" and c.flow == "sot_obj_link_request" for c in out)


def test_build_switch_idempotent_when_already_routed():
    p = _sot_provider()
    ls = LabelStateModel()
    cmds = [Command(command="start_flow", flow="sot_obj_link_request")]
    out, skip = build_transition_commands(
        Decision.SWITCH_FLOW, cmds, Label.SUPPORT_PAYMENT_LINK_REQUEST, ls, p, SOT_FLOWS
    )
    assert out == cmds  # no change


def test_build_clarify_drops_money_start_flow():
    p = _sot_provider()
    ls = LabelStateModel()
    ls = update_evidence(ls, Label.DISPUTE_LOAN_NOT_TAKEN, 1)
    cmds = [Command(command="start_flow", flow="sot_obj_link_request")]
    out, _ = build_transition_commands(
        Decision.CLARIFY_BEFORE_SWITCH, cmds, Label.SUPPORT_PAYMENT_LINK_REQUEST,
        ls, p, SOT_FLOWS,
    )
    assert not any(c.command == "start_flow" and c.flow == "sot_obj_link_request" for c in out)
    assert any(c.command == "clarify" for c in out)


def test_build_block_routes_to_real_risk_flow():
    p = _sot_provider()
    ls = LabelStateModel()
    ls = update_evidence(ls, Label.DISPUTE_LOAN_NOT_TAKEN, 1)
    cmds = [Command(command="start_flow", flow="sot_obj_link_request")]
    out, _ = build_transition_commands(
        Decision.BLOCK_SWITCH_DUE_TO_HIGH_RISK, cmds,
        Label.SUPPORT_PAYMENT_LINK_REQUEST, ls, p, SOT_FLOWS,
    )
    # unresolved dispute.loan_not_taken maps to a real flow -> route there, drop link
    assert any(c.command == "start_flow" and c.flow == "sot_obj_never_loan" for c in out)
    assert not any(c.flow == "sot_obj_link_request" for c in out)


# --- run_label_transition (orchestrator) -----------------------------------------
def test_run_disabled_returns_none_and_unchanged():
    cmds = [Command(command="start_flow", flow="sot_obj_link_request")]
    st = _state()
    st2, out, dec = run_label_transition(
        state=st, commands=cmds, transcript="link bhej do", awaiting_slot="",
        candidate_flows=None, tenant_id="salary_on_time", flows=SOT_FLOWS,
        settings=_settings(label_transition_enabled=False),
    )
    assert dec is None
    assert out == cmds
    assert LABEL_STATE_SLOT not in st2.slots


def test_run_shadow_never_changes_commands():
    cmds = [Command(command="set_slot", name="sot_payment_intent", value="refused")]
    st = _state()
    st2, out, dec = run_label_transition(
        state=st, commands=cmds, transcript="nahi karunga", awaiting_slot="",
        candidate_flows=None, tenant_id="salary_on_time", flows=SOT_FLOWS,
        settings=_settings(label_transition_mode="shadow"),
    )
    assert out == cmds
    assert dec is not None
    assert dec.mode == "shadow"
    assert dec.enforcement_applied is False
    assert st2.slots[LABEL_STATE_SLOT]["active_label"] == Label.REFUSAL_HARD


def test_run_enforce_sot_rewrites_link_when_suppressed():
    # LLM produced nothing routable, but the borrower asked for the link.
    cmds: list[Command] = []
    st = _state()
    # seed a prior refusal so previous_label triggers resolve+switch
    st.slots[LABEL_STATE_SLOT] = LabelStateModel(active_label=Label.REFUSAL_HARD).model_dump(
        mode="json"
    )
    st2, out, dec = run_label_transition(
        state=st, commands=cmds, transcript="sir link bhej do payment ka", awaiting_slot="",
        candidate_flows=None, tenant_id="salary_on_time", flows=SOT_FLOWS,
        settings=_settings(label_transition_mode="enforce"),
    )
    assert dec.mode == "enforce"
    assert dec.enforcement_applied is True
    assert any(c.command == "start_flow" and c.flow == "sot_obj_link_request" for c in out)


def test_run_enforce_unsupported_provider_falls_back_to_shadow():
    cmds = [Command(command="set_slot", name="x", value="y")]
    st = ConversationState(call_id="c", tenant_id="other", borrower_id="b")
    st.attempts = 1
    st2, out, dec = run_label_transition(
        state=st, commands=cmds, transcript="mujhe link chahiye", awaiting_slot="",
        candidate_flows=None, tenant_id="other", flows=SOT_FLOWS,
        settings=_settings(label_transition_mode="enforce", label_transition_scope="all"),
    )
    assert dec.mode == "shadow"
    assert dec.enforcement_skipped_reason == "unsupported_provider"
    assert out == cmds


def test_run_scope_supported_skips_generic_provider():
    cmds = [Command(command="set_slot", name="x", value="y")]
    st = ConversationState(call_id="c", tenant_id="other", borrower_id="b")
    st.attempts = 1
    st2, out, dec = run_label_transition(
        state=st, commands=cmds, transcript="mujhe link chahiye", awaiting_slot="",
        candidate_flows=None, tenant_id="other", flows=SOT_FLOWS,
        settings=_settings(label_transition_scope="supported"),
    )
    assert dec is None
    assert out == cmds


def test_run_enforce_dispute_then_link_clarifies_and_drops_link():
    st = _state()
    # unresolved dispute from a prior turn
    st.slots[LABEL_STATE_SLOT] = LabelStateModel(
        active_label=Label.DISPUTE_LOAN_NOT_TAKEN,
        unresolved_high_risk_labels=[],
    ).model_dump(mode="json")
    # first make it unresolved via evidence by driving a dispute turn
    ls = load_label_state(st)
    ls = update_evidence(ls, Label.DISPUTE_LOAN_NOT_TAKEN, 1)
    st.slots[LABEL_STATE_SLOT] = ls.model_dump(mode="json")

    cmds = [Command(command="start_flow", flow="sot_obj_link_request")]
    st2, out, dec = run_label_transition(
        state=st, commands=cmds, transcript="theek hai link bhej do", awaiting_slot="",
        candidate_flows=None, tenant_id="salary_on_time", flows=SOT_FLOWS,
        settings=_settings(label_transition_mode="enforce"),
    )
    assert dec.decision == Decision.CLARIFY_BEFORE_SWITCH
    assert not any(c.flow == "sot_obj_link_request" for c in out)
    assert any(c.command == "clarify" for c in out)
    assert dec.blocked_by == Label.DISPUTE_LOAN_NOT_TAKEN
