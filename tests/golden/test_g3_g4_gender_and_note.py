"""G3 — {G} resolves by persona voice. G4 — no note/दर्ज commitment in escalation."""

from __future__ import annotations

from app.config import tenant_config
from app.engine.gender import resolve_gender_tokens
from app.engine.nlg import interpolate_template
from app.engine.tracker import new_conversation_state


def test_g3_gender_token_neha_feminine_amit_masculine():
    tmpl = "मैं आपकी बात ठीक से समझ नहीं {G:पा रही|पा रहा} हूँ।"
    assert resolve_gender_tokens(tmpl, "neha") == "मैं आपकी बात ठीक से समझ नहीं पा रही हूँ।"
    assert resolve_gender_tokens(tmpl, "amit") == "मैं आपकी बात ठीक से समझ नहीं पा रहा हूँ।"
    state = new_conversation_state("g3", "paisalo", "b")
    state.slots["voice_id"] = "neha"
    text = interpolate_template(tmpl, dict(state.slots), persona_voice="neha")
    assert "पा रही" in text
    assert "पा रहा" not in text


def test_g4_escalation_reply_is_callback_without_note():
    spoken = tenant_config("paisalo").escalation_reply
    assert "नोट" not in spoken
    assert "दर्ज" not in spoken
    assert "वापस कॉल" in spoken
    assert "{G:" in spoken


def test_g3_escalation_renders_feminine_for_neha():
    cfg = tenant_config("paisalo")
    spoken = resolve_gender_tokens(cfg.escalation_reply, "neha")
    assert "पा रही" in spoken
    assert "पा रहा" not in spoken
    assert "नोट" not in spoken
