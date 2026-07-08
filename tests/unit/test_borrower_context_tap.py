"""Unit tests for CF2.2 tap metadata normalization."""

from app.ws.borrower_context import normalize_borrower_context, parse_tap_only


def test_parse_tap_only_truthy():
    assert parse_tap_only(True) is True
    assert parse_tap_only("true") is True
    assert parse_tap_only("1") is True
    assert parse_tap_only("yes") is True
    assert parse_tap_only(False) is False
    assert parse_tap_only("") is False


def test_normalize_borrower_context_includes_cf2_tap_fields():
    ctx = normalize_borrower_context(
        {
            "speaker_label": "caller",
            "tap_only": True,
            "parent_session_uuid": "main-session-uuid",
        }
    )
    assert ctx["speaker_label"] == "caller"
    assert ctx["tap_only"] is True
    assert ctx["parent_session_uuid"] == "main-session-uuid"
