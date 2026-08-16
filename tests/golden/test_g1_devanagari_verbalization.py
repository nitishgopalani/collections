"""G1 — number/date/phone verbalization is Devanagari, never Latin, never ₹."""

from __future__ import annotations

import re

from app.engine.identity_gate import slots_for_nlg
from app.engine.nlg import (
    interpolate_template,
    spoken_amount_hindi,
    spoken_date_hindi,
    spoken_days_hindi,
    spoken_digits_hindi,
)
from app.engine.tracker import new_conversation_state
from app.flows.loader import get_flow_set

_LATIN = re.compile(r"[A-Za-z]")


def test_g1_amount_words_devanagari_no_rupee_sign():
    spoken = spoken_amount_hindi(4500)
    assert spoken == "चार हज़ार पाँच सौ रुपये"
    assert "₹" not in spoken
    assert _LATIN.search(spoken) is None


def test_g1_days_and_date_and_phone_devanagari():
    assert spoken_days_hindi(15) == "पंद्रह"
    assert _LATIN.search(spoken_days_hindi(15)) is None
    date = spoken_date_hindi("2026-06-26")
    assert date == "छब्बीस जून"
    assert _LATIN.search(date) is None
    phone = spoken_digits_hindi("918035317323")
    assert phone == "नौ एक आठ शून्य तीन पाँच तीन एक सात तीन दो तीन"
    assert _LATIN.search(phone) is None
    assert "ज़ीरो" not in phone


def test_g1_pd1_greeting_renders_devanagari_no_latin_no_rupee():
    flows = get_flow_set()
    state = new_conversation_state("g1-pd1", "paisalo", "plo_test_borrower")
    state.slots.update(
        {
            "customer_name": "रमेश",
            "repay_amount": 4500,
            "days_past_due": 15,
            "identity_ok": True,
            "voice_id": "neha",
        }
    )
    nlg_slots = slots_for_nlg(state.slots)
    variants = flows.responses["plo_pd1_greeting"]
    text = interpolate_template(
        variants[0].text, nlg_slots, channel="voice", persona_voice="neha"
    )
    assert "₹" not in text
    assert _LATIN.search(text) is None
    assert "चार हज़ार पाँच सौ रुपये" in text
    assert "पंद्रह दिनों" in text


def test_g1_derived_days_slot_is_devanagari():
    out = slots_for_nlg({"days_past_due": 15, "identity_ok": True})
    assert out["days_past_due_words"] == "पंद्रह"
    assert _LATIN.search(out["days_past_due_words"]) is None
