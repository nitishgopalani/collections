"""P3.3 — respond fact-grounding guard (hard swap, never partial edit)."""

from app.engine.respond_guard import (
    extract_numeric_tokens,
    ground_respond_text,
    normalize_numeric_fragment,
)

UNKNOWN = "यह सवाल मैं नोट कर रहा हूँ।"


def test_normalize_strips_rupee_commas_spaces():
    assert normalize_numeric_fragment("₹ 2,300") == "2300"


def test_grounded_amount_passes():
    text, result = ground_respond_text(
        "आपकी देय राशि ₹2,300 है।",
        {"repay_amount": 2300, "due_date": "2026-06-30"},
        UNKNOWN,
    )
    assert result == "pass"
    assert text == "आपकी देय राशि ₹2,300 है।"


def test_invented_waiver_swaps_entire_text():
    invented = "हम ₹500 waiver दे सकते हैं, बाकी 1800 भर दीजिए।"
    text, result = ground_respond_text(
        invented,
        {"repay_amount": 2300, "offer_amount": 2000},
        UNKNOWN,
    )
    assert result == "swapped"
    assert text == UNKNOWN
    assert "500" not in text
    assert "waiver" not in text


def test_ungrounded_date_swaps():
    text, result = ground_respond_text(
        "ड्यू डेट 01/01/2099 है।",
        {"repay_amount": 2300, "due_date": "2026-06-30"},
        UNKNOWN,
    )
    assert result == "swapped"
    assert text == UNKNOWN


def test_no_numeric_tokens_passes_verbatim():
    text, result = ground_respond_text(
        "ऑफिस का पता स्लॉट्स में नहीं है।",
        {"repay_amount": 2300},
        UNKNOWN,
    )
    assert result == "pass"
    assert "ऑफिस" in text


def test_extract_numeric_tokens_covers_rupee_and_iso():
    tokens = extract_numeric_tokens("राशि ₹1,200 और ड्यू 2026-06-30")
    assert any("1,200" in t or "1200" in normalize_numeric_fragment(t) for t in tokens)
    assert any("2026-06-30" in t for t in tokens)
