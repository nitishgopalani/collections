import re

import pytest

from app.engine.nlg import (
    MissingSlotError,
    interpolate_template,
    normalize_language,
    pick_variant,
    render,
    spoken_amount_hindi,
    spoken_date_hindi,
    spoken_form_value,
)
from app.engine.tracker import new_conversation_state
from app.flows.loader import load_all_flows
from app.schemas.flow import ResponseTemplate

FLOWS = load_all_flows()


def test_spoken_amount_hindi():
    assert spoken_amount_hindi(12400) == "बारह हज़ार चार सौ रुपये"


def test_spoken_date_hindi():
    assert spoken_date_hindi("2026-06-26") == "छब्बीस जून"


def test_spoken_form_value_whatsapp_skips_spoken():
    assert spoken_form_value(5000, channel="whatsapp") == "5000"


def test_interpolate_slots():
    text = interpolate_template(
        "Pay {amount_due} on {ptp_date}",
        {"amount_due": 5000, "ptp_date": "2026-06-26"},
        channel="whatsapp",
    )
    assert text == "Pay 5000 on 2026-06-26"


def test_interpolate_voice_spoken_form():
    text = interpolate_template(
        "Pay {amount_due} on {ptp_date}",
        {"amount_due": 12400, "ptp_date": "2026-06-26"},
        channel="voice",
    )
    assert "बारह हज़ार चार सौ रुपये" in text
    assert "छब्बीस जून" in text
    assert "₹" not in text
    assert not re.search(r"[A-Za-z]", "बारह हज़ार चार सौ रुपये")
    assert not re.search(r"[A-Za-z]", "छब्बीस जून")


def test_missing_slot_raises():
    with pytest.raises(MissingSlotError):
        interpolate_template("Hello {missing_slot}", {"other": 1})


def test_language_select_from_comms_prefs():
    state = new_conversation_state("c", "default", "b")
    state.slots["comms_prefs"] = {"language": "en"}
    assert normalize_language("hi-IN", state) == "en"


def test_language_select_from_locale():
    state = new_conversation_state("c", "default", "b")
    assert normalize_language("en-IN", state) == "en"


def test_render_hindi_variant():
    state = new_conversation_state("c", "default", "b")
    state.slots["ptp_date"] = "2026-06-27"
    state.slots["amount_due"] = 5000
    state.slots["identity_ok"] = True
    state.slots["comms_prefs"] = {"language": "hi"}
    text = render("confirm_ptp", state, FLOWS, locale="hi-IN", channel="whatsapp")
    assert "Theek hai sir" in text
    assert "5000" in text


def test_render_english_variant():
    state = new_conversation_state("c", "default", "b")
    state.slots["ptp_date"] = "2026-06-27"
    state.slots["amount_due"] = 5000
    state.slots["identity_ok"] = True
    state.slots["comms_prefs"] = {"language": "en"}
    text = render("confirm_ptp", state, FLOWS, locale="en-IN", channel="whatsapp")
    assert text.startswith("Okay sir")


def test_variant_rotation_changes_text():
    variants = [
        ResponseTemplate(text="A", language="hi"),
        ResponseTemplate(text="B", language="hi"),
        ResponseTemplate(text="C", language="hi"),
    ]
    picks = [
        pick_variant(variants, preferred_language="hi", rotation_index=i).text for i in range(3)
    ]
    assert picks == ["A", "B", "C"]
    assert picks[0] != picks[1]


def test_render_full_pipeline_with_executor_reply():
    from app.clients.tools_sim import FakeToolClient
    from app.engine.actions import make_action_runner
    from app.engine.executor import run
    from app.engine.tracker import apply
    from app.schemas.command import Command

    runner = make_action_runner(FakeToolClient())
    state = new_conversation_state("c-nlg", "default", "b")
    state.slots["call_date"] = "2026-06-25"
    state.slots["amount_due"] = 5000
    state.slots["identity_ok"] = True
    state = apply(state, [Command(command="start_flow", flow="promise_to_pay")])
    state = apply(state, [Command(command="set_slot", name="ptp_date", value="2026-06-27")])
    result = run(state, FLOWS, runner)
    reply = render(result.reply_id, result.state, FLOWS, channel="whatsapp")
    assert "2026-06-27" in reply
    assert "5000" in reply
