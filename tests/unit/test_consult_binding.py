"""Unit tests for the property-leg consult binding registry (TTL, uuid forms)."""

from __future__ import annotations

import time

import pytest

from app.engine import consult_binding


@pytest.fixture(autouse=True)
def _clean_bindings():
    consult_binding.reset()
    yield
    consult_binding.reset()


CTX = {
    "tenant_id": "booking-confirm",
    "persona": "persona_property",
    "booking_id": "BK123",
    "hotel": "Hotel Sunrise",
    "guest": "Rahul",
}


def test_lookup_matches_dashed_and_dashless_uuid_forms():
    consult_binding.register("AAAABBBB-CCCC-4DDD-8EEE-FFFF00001111", CTX)
    # The connector strips dashes and the orchestrator uses lowercase-with-
    # dashes; both spellings must hit the same binding.
    for form in (
        "aaaabbbb-cccc-4ddd-8eee-ffff00001111",
        "aaaabbbbcccc4ddd8eeeffff00001111",
        "AAAABBBBCCCC4DDD8EEEFFFF00001111",
    ):
        found = consult_binding.lookup(form)
        assert found is not None, form
        assert found["booking_id"] == "BK123"


def test_lookup_is_non_destructive_until_unregister():
    consult_binding.register("11112222-3333-4444-5555-666677778888", CTX)
    assert consult_binding.lookup("11112222333344445555666677778888") is not None
    # A reconnect (second session_start with the same uuid) still binds.
    assert consult_binding.lookup("11112222333344445555666677778888") is not None
    consult_binding.unregister("11112222-3333-4444-5555-666677778888")
    assert consult_binding.lookup("11112222333344445555666677778888") is None


def test_unknown_uuid_returns_none():
    assert consult_binding.lookup("d7166ff452214a78b40c05b88f6cf5e6") is None


def test_empty_uuid_never_registers():
    consult_binding.register("", CTX)
    assert consult_binding.lookup("") is None


def test_ttl_expires_bindings_when_consult_never_connects(monkeypatch):
    """If the consult leg never connects (telco reject / no answer), the
    binding must not leak: it expires after CONSULT_BINDING_TTL_S."""
    monkeypatch.setenv("CONSULT_BINDING_TTL_S", "0.05")
    consult_binding.register("99998888-7777-4666-8555-444433332222", CTX)
    assert consult_binding.lookup("99998888777746668555444433332222") is not None
    time.sleep(0.08)
    assert consult_binding.lookup("99998888777746668555444433332222") is None


def test_registered_context_is_copied_not_aliased():
    ctx = dict(CTX)
    consult_binding.register("12341234-1234-4234-8234-123412341234", ctx)
    ctx["booking_id"] = "MUTATED"
    found = consult_binding.lookup("12341234123442348234123412341234")
    assert found is not None and found["booking_id"] == "BK123"
    # And the returned copy cannot mutate the stored context either.
    found["booking_id"] = "MUTATED-2"
    again = consult_binding.lookup("12341234123442348234123412341234")
    assert again is not None and again["booking_id"] == "BK123"
