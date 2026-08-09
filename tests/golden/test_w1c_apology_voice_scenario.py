"""W1-C C0 / F3 (PREDUE-007 residual): apology_voice_id resolves to the
PaisaLo per-scenario voice at session_start.

The static ``profile.voice_id`` is empty for PaisaLo (voice is per-scenario,
selected by ``select_plo_scenario`` in the opener). At session_start the
opener has not run yet, so the brain must resolve the scenario voice from
the hydrated borrower loan (dpd/npa) and carry it in ``session_ready.
apology_voice_id`` — otherwise DeadAirHandler speaks the apology in the
default voice, not the call's voice (predue/ondue→priya, postdue1/2→neha,
postdue3→kabir, npa→amit).
"""

from app.schemas.state import BorrowerRecord
from app.ws.handler import _resolve_plo_scenario_voice


class _Settings:
    def __init__(self, test_plo_scenario: str = ""):
        self.test_plo_scenario = test_plo_scenario


def _borrower(dpd, npa=False) -> BorrowerRecord:
    return BorrowerRecord(
        borrower_id="b",
        identity={},
        loan={"days_past_due": dpd, "dpd": dpd, "npa_flag": npa},
    )


def test_f3_predue_resolves_priya():
    # dpd<0 → predue → priya
    assert _resolve_plo_scenario_voice(_borrower(-5), _Settings()) == "priya"


def test_f3_ondue_resolves_priya():
    # dpd=0 → ondue → priya
    assert _resolve_plo_scenario_voice(_borrower(0), _Settings()) == "priya"


def test_f3_postdue1_resolves_neha():
    assert _resolve_plo_scenario_voice(_borrower(15), _Settings()) == "neha"


def test_f3_postdue3_resolves_kabir():
    assert _resolve_plo_scenario_voice(_borrower(75), _Settings()) == "kabir"


def test_f3_npa_resolves_amit():
    assert _resolve_plo_scenario_voice(_borrower(120, npa=True), _Settings()) == "amit"


def test_f3_override_wins():
    # TEST_PLO_SCENARIO override beats dpd bucket
    assert _resolve_plo_scenario_voice(_borrower(15), _Settings("predue")) == "priya"


def test_f3_missing_loan_fields_falls_back_neha():
    # No dpd/npa → dpd=0 → ondue → priya (not neha). Empty loan → dpd=0.
    rec = BorrowerRecord(borrower_id="b", identity={}, loan={})
    assert _resolve_plo_scenario_voice(rec, _Settings()) == "priya"


def test_f3_non_numeric_dpd_falls_back_to_ondue():
    rec = BorrowerRecord(
        borrower_id="b", identity={}, loan={"days_past_due": "n/a", "npa_flag": False}
    )
    assert _resolve_plo_scenario_voice(rec, _Settings()) == "priya"
