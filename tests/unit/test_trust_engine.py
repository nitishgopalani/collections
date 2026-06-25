"""Sprint 8 — Trust Score Engine tests (blueprint §10.3 worked paths)."""

from datetime import UTC, datetime

import pytest

from app.config import tenant_config
from app.engine.gate import gate
from app.engine.tracker import hydrate_from_borrower, new_conversation_state
from app.engines_p2.trust import (
    NEUTRAL_TRUST,
    TRUST_IS_INPUT_NOT_LICENSE,
    TrustEvent,
    accumulate_trust,
    compute_trust_score,
    refresh_borrower_trust,
    sync_trust_on_persist,
)
from app.schemas.state import BorrowerRecord
from tests.fixtures.trust_blueprint_paths import (
    PATH_TARGETS,
    gaming_borrower,
    gaming_borrower_without_final_payment,
    recovering_borrower,
    reliable_borrower,
    slipping_borrower,
)

# --- Blueprint §10.3 worked paths ---


def test_reliable_borrower_path_50_to_88():
    score = compute_trust_score(reliable_borrower()).score
    assert score == PATH_TARGETS["reliable"]


def test_slipping_borrower_path_60_to_32():
    score = compute_trust_score(slipping_borrower()).score
    assert score == PATH_TARGETS["slipping"]


def test_recovering_borrower_path_35_to_58():
    score = compute_trust_score(recovering_borrower()).score
    assert score == PATH_TARGETS["recovering"]


def test_gaming_anchored_path_22_to_30():
    score = compute_trust_score(gaming_borrower()).score
    assert score == PATH_TARGETS["gaming"]


def test_determinism_same_history_same_score():
    borrower = reliable_borrower()
    first = compute_trust_score(borrower).score
    second = compute_trust_score(borrower).score
    assert first == second == PATH_TARGETS["reliable"]


def test_bounds_always_0_to_100():
    extreme = BorrowerRecord(
        borrower_id="B_EXTREME",
        broken_ptps=[
            {"promised_date": f"2026-01-{d:02d}", "broken_on": f"2026-01-{d:02d}", "magnitude": 1.0}
            for d in range(1, 15)
        ],
        payments=[
            {"date": "2026-01-20", "on_time": True, "full": True, "magnitude": 1.0},
        ],
    )
    score = compute_trust_score(extreme).score
    assert 0 <= score <= 100


def test_trust_history_appended_on_refresh():
    borrower = reliable_borrower()
    updated = refresh_borrower_trust(borrower, trigger="unit_test")
    assert updated.trust_current == PATH_TARGETS["reliable"]
    assert len(updated.trust_history) >= 1
    assert updated.trust_history[-1]["score"] == PATH_TARGETS["reliable"]
    assert updated.trust_history[-1]["trigger"] == "unit_test"


def test_hydrate_exposes_trust_slot():
    borrower = refresh_borrower_trust(reliable_borrower())
    state = new_conversation_state("c", "default", borrower.borrower_id)
    hydrated = hydrate_from_borrower(state, borrower)
    assert hydrated.slots["trust"] == PATH_TARGETS["reliable"]


def test_sync_trust_on_persist():
    borrower = sync_trust_on_persist(gaming_borrower())
    assert borrower.trust_current == PATH_TARGETS["gaming"]


def test_trust_is_input_not_license_constant():
    assert TRUST_IS_INPUT_NOT_LICENSE is True


@pytest.mark.compliance
def test_high_trust_never_relaxes_compliance_gate():
    """Trust is an input, not a license — gate blocks prohibited conduct regardless."""
    state = new_conversation_state("c", "default", "b")
    state.slots["trust"] = 100
    state.slots["compliance_flags"] = {}
    cfg = tenant_config("default")
    now = datetime(2026, 6, 25, 10, 0, tzinfo=UTC)
    draft = "Police aa jayegi agar payment nahi"
    result = gate(draft, state, cfg, now=now)
    assert result.verdict == "modify"
    assert result.level == "CRITICAL"
    assert "police" not in result.text.lower()


@pytest.mark.compliance
def test_high_trust_does_not_bypass_opt_out():
    state = new_conversation_state("c", "default", "b")
    state.slots["trust"] = 100
    state.slots["compliance_flags"] = {"opt_out": True}
    cfg = tenant_config("default")
    now = datetime(2026, 6, 25, 10, 0, tzinfo=UTC)
    result = gate("Namaste sir EMI ke liye", state, cfg, now=now)
    assert result.verdict == "block"
    assert result.reason == "opt_out_active"


def test_accumulator_respects_recency():
    older = TrustEvent("broken_promise", datetime(2026, 1, 1, tzinfo=UTC), 1.0)
    newer = TrustEvent("promise_kept", datetime(2026, 2, 1, tzinfo=UTC), 1.0)
    with_newer = accumulate_trust([older, newer], initial=NEUTRAL_TRUST)
    reversed_order = accumulate_trust([newer, older], initial=NEUTRAL_TRUST)
    assert with_newer == reversed_order
    assert with_newer > accumulate_trust([older], initial=NEUTRAL_TRUST)


def test_broken_promise_penalty_exceeds_kept_reward():
    kept = _event_delta_only("promise_kept", 1.0)
    broken = abs(_event_delta_only("broken_promise", 1.0))
    assert broken > kept


def _event_delta_only(event_type: str, magnitude: float) -> float:
    from app.engines_p2.trust import _event_delta

    return _event_delta(event_type, magnitude)


def test_anchor_limits_gaming_single_payment():
    score_without = compute_trust_score(gaming_borrower_without_final_payment()).score
    score_with = compute_trust_score(gaming_borrower()).score
    assert score_without < score_with
    assert score_with == PATH_TARGETS["gaming"]
    assert score_without <= 22
    assert score_with - score_without <= 16
