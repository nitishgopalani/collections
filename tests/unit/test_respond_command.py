"""P3.1 — respond command schema validation (set_slot ok; start_flow drops respond)."""

import json

from app.engine.command_gen import RESPOND_MAX_CHARS, parse_and_validate_commands


def test_respond_with_set_slot_kept():
    raw = json.dumps(
        [
            {"command": "respond", "text": "आपकी देय राशि 2300 है।"},
            {"command": "set_slot", "name": "sot_payment_intent", "value": "willing"},
        ]
    )
    result = parse_and_validate_commands(raw, respond_enabled=True)
    types = {c.command for c in result.commands}
    assert "respond" in types
    assert "set_slot" in types
    assert not any("dropped respond" in r for r in result.rejections)


def test_respond_with_start_flow_drops_respond():
    raw = json.dumps(
        [
            {"command": "respond", "text": "आपकी देय राशि 2300 है।"},
            {"command": "start_flow", "flow": "sot_obj_link_request"},
        ]
    )
    result = parse_and_validate_commands(raw, respond_enabled=True)
    types = [c.command for c in result.commands]
    assert "respond" not in types
    assert "start_flow" in types
    assert any("dropped respond co-occurring with start_flow" in r for r in result.rejections)


def test_respond_rejected_when_disabled():
    raw = json.dumps([{"command": "respond", "text": "hello"}])
    result = parse_and_validate_commands(raw, respond_enabled=False)
    assert all(c.command != "respond" for c in result.commands)
    assert any("respond_enabled=false" in r for r in result.rejections)


def test_respond_rejects_over_max_chars():
    raw = json.dumps(
        [{"command": "respond", "text": "x" * (RESPOND_MAX_CHARS + 1)}]
    )
    result = parse_and_validate_commands(raw, respond_enabled=True)
    assert all(c.command != "respond" for c in result.commands)
    assert any(f"over {RESPOND_MAX_CHARS}" in r for r in result.rejections)


def test_respond_strips_markdown_and_newlines():
    raw = json.dumps(
        [{"command": "respond", "text": "**राशि** 2300\nहै।"}]
    )
    result = parse_and_validate_commands(raw, respond_enabled=True)
    respond = next(c for c in result.commands if c.command == "respond")
    assert "\n" not in (respond.text or "")
    assert "*" not in (respond.text or "")
    assert "2300" in (respond.text or "")
