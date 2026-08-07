"""P5.4 — Hindi legal/NPA pressure lexicon (nukta-insensitive)."""

from __future__ import annotations

from app.compliance_defaults import COLLECTION_PRESSURE_PHRASES
from app.engine.compliance_rules import (
    evaluate_pressure_with_allowlist,
    matches_any,
    normalize,
)


def test_nukta_stripped_in_normalize():
    assert normalize("सख़्त") == normalize("सख्त")
    assert normalize("डिफ़ॉल्ट") == normalize("डिफॉल्ट")


def test_hindi_family_in_defaults():
    assert "कानूनी कार्रवाई" in COLLECTION_PRESSURE_PHRASES
    assert "खाता NPA" in COLLECTION_PRESSURE_PHRASES
    assert "NPA घोषित" in COLLECTION_PRESSURE_PHRASES


def test_sakht_nukta_variant_matches_lexicon():
    # Lexicon has सख़्त; copy may use सख्त — nukta-insensitive normalize unifies.
    text = "आपके विरुद्ध सख्त कार्रवाई की जाएगी"
    assert matches_any(text, list(COLLECTION_PRESSURE_PHRASES)) in {
        "सख़्त कार्रवाई",
        "सख्त कार्रवाई",
    }


def test_default_nukta_variant_matches():
    text = "इससे डिफ़ॉल्ट हो सकता है"
    hit = matches_any(text, list(COLLECTION_PRESSURE_PHRASES))
    assert hit == "डिफॉल्ट हो सकता"


def test_allowlist_covers_hindi_pressure_span():
    text = "भुगतान ना करने पर कानूनी कार्रवाई हो सकती है"
    phrases = list(COLLECTION_PRESSURE_PHRASES)
    blocking, warnings = evaluate_pressure_with_allowlist(text, phrases, [])
    assert blocking == "कानूनी कार्रवाई"
    blocking2, warnings2 = evaluate_pressure_with_allowlist(
        text, phrases, ["कानूनी कार्रवाई"]
    )
    assert blocking2 is None
    assert warnings2
    assert warnings2[0]["allowlisted"] is True


def test_emi_removed_from_pressure_lexicon():
    assert "emi" not in COLLECTION_PRESSURE_PHRASES


def test_emi_plus_hindi_allowlisted_warning_only():
    """CP5 F1(d): EMI product copy + Hindi legal → warn via allowlist, no block."""
    from app.engine.tenant_profile import clear_tenant_profile_cache, get_tenant_profile

    clear_tenant_profile_cache()
    profile = get_tenant_profile("paisalo")
    assert profile is not None
    text = (
        "कृपया ध्यान दें कि आप पहले ही EMI भुगतान में देरी कर चुके हैं। "
        "और देर होने पर आपका खाता NPA में जा सकता है, और कानूनी कार्रवाई भी हो सकती है।"
    )
    blocking, warnings = evaluate_pressure_with_allowlist(
        text,
        list(COLLECTION_PRESSURE_PHRASES),
        list(profile.gate_allowlisted_phrases),
    )
    assert blocking is None
    assert warnings
    assert all(w.get("allowlisted") for w in warnings)
    assert {w.get("phrase") for w in warnings} <= set(profile.gate_allowlisted_phrases) | {
        "खाता NPA",
        "कानूनी कार्रवाई",
        "NPA घोषित",
    }
