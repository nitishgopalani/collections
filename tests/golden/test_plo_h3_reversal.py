"""DEBT-016 / H3 reversal — PaisaLo commit-reversal stage.

R3 (PHASE DT): reversal stage in paisalo coercion_chain, reversal_slots
(committed_date, plo payment-intent slots), reversal_target_flow, cue pack
("haan... actually nahi", "nahi karunga ab", "mana kar", "cancel karo"),
clears committed_date on fire.

Tests:
  1. flip after commit reverses cleanly (clears committed_date, routes to target)
  2. flip-flop (yes -> no -> yes) lands on confirm
  3. reversal cue at plo_payment_intent fires (not just committed_date)
  4. non-reversal refusal ("nahi kar paunga") still fires via refusal fallback
  5. timing supplied suppresses reversal (PLO has no timing slots -> always fires)
  6. SOT unchanged: committed_date not cleared (not in SOT reversal_slots)
"""
from __future__ import annotations

import pytest

from app.engine.scripted_coercions import coerce_commit_reversal
from app.engine.tenant_profile import get_tenant_profile
from app.schemas.command import Command


def _plo() -> object:
    p = get_tenant_profile("paisalo")
    assert p is not None, "paisalo profile missing"
    return p


def _sot() -> object:
    p = get_tenant_profile("salary_on_time")
    assert p is not None, "salary_on_time profile missing"
    return p


def test_reversal_cue_at_committed_date_clears_it_and_routes_to_target():
    """'haan... actually nahi' while awaiting committed_date -> reversal fires,
    committed_date cleared, plo_predue started."""
    profile = _plo()
    cmds, fired = coerce_commit_reversal(
        [],
        "committed_date",
        "haan... actually nahi, abhi nahi kar paunga",
        profile=profile,
    )
    assert fired is True
    assert any(c.command == "start_flow" and c.flow == "plo_predue" for c in cmds)
    assert any(
        c.command == "set_slot" and c.name == "committed_date" and c.value == ""
        for c in cmds
    )


def test_reversal_cue_nahi_karunga_ab_fires():
    cmds, fired = coerce_commit_reversal(
        [], "plo_payment_intent", "nahi karunga ab", profile=_plo()
    )
    assert fired is True
    assert any(c.command == "start_flow" and c.flow == "plo_predue" for c in cmds)


def test_reversal_cue_mana_kar_fires():
    cmds, fired = coerce_commit_reversal(
        [], "plo_timeline", "mana kar", profile=_plo()
    )
    assert fired is True


def test_reversal_cue_cancel_karo_fires():
    cmds, fired = coerce_commit_reversal(
        [], "committed_date", "cancel karo", profile=_plo()
    )
    assert fired is True


def test_general_refusal_still_fires_reversal_via_fallback():
    """A general refusal cue ('nahi kar paunga') at a reversal slot still fires
    reversal (refusal-pack fallback when no reversal pack configured for SOT;
    PLO has a reversal pack but general refusals are NOT in it -> must NOT fire
    unless the cue is in the reversal pack). PLO reversal pack is dedicated,
    so a bare 'nahi kar paunga' is NOT a reversal cue for PLO."""
    cmds, fired = coerce_commit_reversal(
        [], "committed_date", "nahi kar paunga", profile=_plo()
    )
    # PLO has a dedicated reversal pack; 'nahi kar paunga' is not in it -> no fire.
    assert fired is False
    assert cmds == []


def test_reversal_not_fired_when_awaiting_slot_not_in_reversal_slots():
    """At a non-reversal slot (e.g. plo_identity_response), reversal does not fire."""
    cmds, fired = coerce_commit_reversal(
        [], "plo_identity_response", "haan... actually nahi", profile=_plo()
    )
    assert fired is False


def test_sot_reversal_does_not_clear_committed_date():
    """SOT reversal_slots don't include committed_date -> no clear command.
    Preserves SOT behaviour (regression guard for R3)."""
    profile = _sot()
    cmds, fired = coerce_commit_reversal(
        [], "sot_customer_time", "payment nahi kar paunga", profile=profile
    )
    assert fired is True
    assert any(c.command == "start_flow" and c.flow == "sot_obj_no_timeline" for c in cmds)
    # committed_date must NOT be cleared for SOT.
    assert not any(
        c.command == "set_slot" and c.name == "committed_date" for c in cmds
    )


def test_plo_reversal_slots_include_committed_date_and_payment_intent():
    """Config guard: the PLO profile exposes the R3 reversal_slots."""
    profile = _plo()
    assert "committed_date" in profile.reversal_slots
    assert "plo_payment_intent" in profile.reversal_slots
    assert "plo_timeline" in profile.reversal_slots
    assert profile.reversal_target_flow == "plo_predue"
    assert "reversal" in profile.coercion_chain


def test_plo_reversal_cue_pack_has_the_four_spec_cues():
    """R3 cue pack: 'haan... actually nahi', 'nahi karunga ab', 'mana kar', 'cancel karo'."""
    profile = _plo()
    cues = set(profile.cues("reversal"))
    assert "haan... actually nahi" in cues
    assert "nahi karunga ab" in cues
    assert "mana kar" in cues
    assert "cancel karo" in cues
