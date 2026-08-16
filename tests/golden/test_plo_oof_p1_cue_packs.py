"""PLO-OOF P1 — PaisaLo Tier-1 cue packs (willing / refusal / callback).

Regression for the live fb6a0f02 turn-6 bug: transcript "ठीक है" at the
plo_payment_intent collect step must coerce to ``willing`` and advance to
the assurance path (plo_predue_ack), NOT emit a clarify.

Covers:
- willing pack reuses SOT list (theek hai / ठीक / haan / kar dunga / ho
  jayega / bilkul / zaroor + Devanagari forms) + disqualifiers.
- refusal: cue + shared INABILITY_RE both apply for paisalo.
- callback-request cues route to plo_obj_callback_pd.
- coercion_chain config includes willing + refusal (pasted in test).
"""

from __future__ import annotations

import json

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.retrieval import clear_retrieval_cache
from app.engine.scripted_coercions import (
    coerce_callback_request,
    coerce_identity,
    coerce_payment_refusal,
    coerce_push_willing,
    run_coercion_chain,
)
from app.engine.tenant_profile import clear_tenant_profile_cache, get_tenant_profile
from app.engine.turn import handle_turn
from app.flows.loader import reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest

CALL_DATE = "2026-08-06"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("SCRIPTED_CATALOG_ROUTING", "true")
    monkeypatch.setenv("CALL_WINDOW_START", "00:00")
    monkeypatch.setenv("CALL_WINDOW_END", "23:59")
    clear_tenant_profile_cache()
    get_settings.cache_clear()
    reload_flow_set()
    clear_retrieval_cache()
    yield
    clear_retrieval_cache()
    clear_tenant_profile_cache()
    get_settings.cache_clear()


class _EmptyKB:
    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def retrieve(self, text, tenant_id, k: int = 6):
        return []


class _ScriptedLLM:
    def __init__(self, turns):
        self._responses = [json.dumps(t) for t in turns]
        self.call_count = 0

    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def complete(self, system, user, *, json_only=True, **kw) -> str:
        self.call_count += 1
        if self.call_count <= len(self._responses):
            return self._responses[self.call_count - 1]
        return "[]"


async def _turn(memory, call_id, text, llm, *, turn_meta=None):
    req = TurnRequest(
        call_id=call_id,
        borrower_id="plo_test_borrower",
        tenant_id="paisalo",
        channel="voice",
        locale="hi-IN",
        transcript=text,
        turn_meta=turn_meta or {"force_flow": "plo_opener", "call_date": CALL_DATE},
    )
    return await handle_turn(req, memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB())


# ---------------------------------------------------------------------------
# Unit: coercion_chain config + cue packs
# ---------------------------------------------------------------------------


def test_p1_paisalo_coercion_chain_includes_willing_and_refusal():
    """Verify (paste) the current paisalo coercion_chain includes willing + refusal.

    DT R3 (DEBT-016): ``reversal`` stage added after ``identity`` (H3 reversal).
    """
    profile = get_tenant_profile("paisalo")
    assert profile is not None
    chain = list(profile.coercion_chain)
    # Pasted current chain (post-DT R3):
    #   dispute -> callback -> willing -> refusal -> identity -> reversal -> reason_catchall
    assert chain == [
        "dispute",
        "callback",
        "willing",
        "refusal",
        "identity",
        "reversal",
        "reason_catchall",
    ], chain
    assert "willing" in chain
    assert "refusal" in chain
    assert "callback" in chain


def test_p1_paisalo_willing_pack_reuses_sot_cues():
    """theek hai / ठीक / haan / kar dunga / ho jayega / bilkul / zaroor all present."""
    profile = get_tenant_profile("paisalo")
    assert profile is not None
    willing = set(profile.cues("willing"))
    for cue in (
        "theek hai",
        "thik hai",
        "theek",
        "haan",
        "kar dunga",
        "ho jayega",
        "bilkul",
        "zaroor",
        "ठीक",
        "हाँ",
        "कर दूंगा",
        "हो जाएगा",
        "बिल्कुल",
        "ज़रूर",
    ):
        assert cue in willing, f"missing willing cue: {cue!r}"


def test_p1_paisalo_willing_disqualifiers_present():
    """kal / baad me / nahi ... disqualify a willing read."""
    profile = get_tenant_profile("paisalo")
    assert profile is not None
    disq = set(profile.cues("willing_disqualifiers"))
    for cue in ("kal", "baad me", "baad mein", "nahi", "nahin", "कल", "बाद में", "नहीं"):
        assert cue in disq, f"missing disqualifier: {cue!r}"


def test_p1_paisalo_callback_request_cue_pack_present():
    profile = get_tenant_profile("paisalo")
    assert profile is not None
    cues = set(profile.cues("callback_request"))
    for cue in (
        "baad mein call karo",
        "busy hoon",
        "abhi time nahi",
        "बाद में कॉल",
        "व्यस्त",
    ):
        assert cue in cues, f"missing callback cue: {cue!r}"
    assert profile.callback_flow == "plo_obj_callback_pd"


# ---------------------------------------------------------------------------
# Unit: coerce_push_willing / coerce_payment_refusal / coerce_callback_request
# ---------------------------------------------------------------------------


def test_p1_theek_hai_coerces_willing_via_cue():
    """Unit: "ठीक है" at plo_payment_intent → coerce_push_willing fires willing."""
    profile = get_tenant_profile("paisalo")
    cmds, fired = coerce_push_willing([], "plo_payment_intent", "ठीक है", profile=profile)
    assert fired is True
    assert any(c.command == "set_slot" and c.name == "plo_payment_intent" and c.value == "willing" for c in cmds)


def test_p1_kal_de_dunga_disqualified_not_willing():
    """Disqualifier "kal" blocks the willing read even when "de dunga" is present."""
    profile = get_tenant_profile("paisalo")
    cmds, fired = coerce_push_willing([], "plo_payment_intent", "kal de dunga", profile=profile)
    assert fired is False
    assert not any(c.command == "set_slot" and c.value == "willing" for c in cmds)


def test_p1_nahi_ho_payega_coerces_refusal_via_cue():
    """Unit: "nahi ho payega" → refusal via cue (cue wins over regex)."""
    profile = get_tenant_profile("paisalo")
    cmds, fired, via, _cls = coerce_payment_refusal(
        [], "plo_payment_intent", "nahi ho payega", profile=profile
    )
    assert fired is True
    assert via == "cue"
    assert any(c.command == "set_slot" and c.name == "plo_payment_intent" and c.value == "refused" for c in cmds)


def test_p1_inability_regex_applies_for_paisalo():
    """Shared INABILITY_RE fires for a paisalo slot on a regex-only refusal."""
    profile = get_tenant_profile("paisalo")
    # "नहीं ... पाएगी" matches INABILITY_RE but no intent_refusal cue substring.
    cmds, fired, via, _cls = coerce_payment_refusal(
        [], "plo_payment_intent", "आज नहीं चल पाएगी", profile=profile
    )
    assert fired is True
    assert via == "regex"


def test_p1_callback_request_routes_to_plo_callback_flow():
    """Unit: "baad mein call karo" → start_flow plo_obj_callback_pd."""
    profile = get_tenant_profile("paisalo")
    cmds, fired = coerce_callback_request(
        [], "baad mein call karo", on_rails=True, profile=profile
    )
    assert fired is True
    assert any(c.command == "start_flow" and c.flow == "plo_obj_callback_pd" for c in cmds)
    assert any(
        c.command == "set_slot" and c.name == "disposition" and c.value == "callback_request"
        for c in cmds
    )


def test_p1_callback_request_busy_hoon_routes():
    profile = get_tenant_profile("paisalo")
    cmds, fired = coerce_callback_request(
        [], "abhi busy hoon baad mein baat karte", on_rails=True, profile=profile
    )
    assert fired is True
    assert cmds[0].flow == "plo_obj_callback_pd"


def test_p1_callback_request_off_rails_does_not_fire():
    profile = get_tenant_profile("paisalo")
    cmds, fired = coerce_callback_request(
        [], "baad mein call karo", on_rails=False, profile=profile
    )
    assert fired is False


def test_p1_run_coercion_chain_callback_beats_willing():
    """Chain order: callback fires before willing (busy → callback, not willing)."""
    profile = get_tenant_profile("paisalo")
    # "busy hoon" contains no willing cue, but proves callback wins the order slot.
    cmds, meta = run_coercion_chain(
        [], "plo_payment_intent", "busy hoon abhi", profile=profile, on_rails=True, blank_transcript=False
    )
    assert any(c.command == "start_flow" and c.flow == "plo_obj_callback_pd" for c in cmds)


def test_p1_callback_at_npa_timeline_not_swallowed_by_catchall():
    """NPA awaits plo_timeline; catchall must not overwrite callback start_flow."""
    profile = get_tenant_profile("paisalo")
    cmds, _meta = run_coercion_chain(
        [],
        "plo_timeline",
        "baad mein call karna",
        profile=profile,
        on_rails=True,
        blank_transcript=False,
        scenario="npa",
    )
    assert any(c.command == "start_flow" and c.flow == "plo_obj_npa_callback" for c in cmds)
    assert not any(c.command == "set_slot" and c.name == "plo_timeline" for c in cmds)


# ---------------------------------------------------------------------------
# Integration: turn-6 "ठीक है" → willing → assurance (plo_predue_ack), NO clarify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p1_theek_hai_at_plo_payment_intent_advances_to_assurance(monkeypatch):
    """Regression for fb6a0f02 turn-6: "ठीक है" → willing → plo_predue_ack.

    Setup: predue scenario, identity confirmed, then at the plo_payment_intent
    collect step the borrower says "ठीक है" and the LLM returns NO command
    (empty). The willing coercion must set plo_payment_intent=willing, the flow
    must advance to ack_willing → plo_predue_ack, and no clarify may be spoken.
    """
    monkeypatch.setenv("TEST_PLO_SCENARIO", "predue")
    get_settings.cache_clear()
    memory = InMemoryMemoryStore()
    call_id = "plo-oof-p1-theek-hai"
    llm = _ScriptedLLM(
        [
            [],  # t1 greeting — no commands
            [{"command": "set_slot", "name": "plo_identity_response", "value": "confirmed"}],
            [],  # t3 at plo_payment_intent — empty LLM; coercion must carry
        ]
    )

    await _turn(memory, call_id, "", llm)  # t1 greeting
    await _turn(memory, call_id, "haan", llm)  # t2 identity confirmed
    r3 = await _turn(memory, call_id, "ठीक है", llm)  # t3 payment intent

    state = await memory.load_state(call_id)
    assert state.slots.get("plo_payment_intent") == "willing"
    # Assurance path = plo_predue_ack (ack_willing utter), NOT plo_reask_intent.
    assert r3.reply_id == "plo_predue_ack", r3.reply_id
    # No clarify command may survive into the spoken reply.
    assert "clarify" not in (r3.reply_text or "").lower()


@pytest.mark.asyncio
async def test_p1_nahi_ho_payega_at_plo_payment_intent_routes_refusal(monkeypatch):
    """Refusal path: "नहीं हो पाएगा" → plo_payment_intent=refused → soft_push / close."""
    monkeypatch.setenv("TEST_PLO_SCENARIO", "predue")
    get_settings.cache_clear()
    memory = InMemoryMemoryStore()
    call_id = "plo-oof-p1-refusal"
    llm = _ScriptedLLM(
        [
            [],
            [{"command": "set_slot", "name": "plo_identity_response", "value": "confirmed"}],
            [],  # empty LLM; refusal coercion must set refused
        ]
    )

    await _turn(memory, call_id, "", llm)
    await _turn(memory, call_id, "haan", llm)
    r3 = await _turn(memory, call_id, "नहीं हो पाएगा", llm)

    state = await memory.load_state(call_id)
    assert state.slots.get("plo_payment_intent") == "refused"
    # Refusal on predue routes to soft_push (plo_predue_push) — NOT ack_willing.
    assert r3.reply_id != "plo_predue_ack", r3.reply_id


@pytest.mark.asyncio
async def test_p1_callback_request_at_plo_payment_intent_routes_to_callback_flow(monkeypatch):
    """Integration: "busy hoon baad mein call karo" → callback flow speaks its line.

    The plo_obj_callback_pd flow is a one-shot deflection utterance (single
    ``say`` step → ``next: end``): it speaks the callback line and does not
    stay on the flow stack. Assert the spoken reply_id + text instead.
    """
    monkeypatch.setenv("TEST_PLO_SCENARIO", "predue")
    get_settings.cache_clear()
    memory = InMemoryMemoryStore()
    call_id = "plo-oof-p1-callback"
    llm = _ScriptedLLM(
        [
            [],
            [{"command": "set_slot", "name": "plo_identity_response", "value": "confirmed"}],
            [],  # empty LLM; callback coercion must route
        ]
    )

    await _turn(memory, call_id, "", llm)
    await _turn(memory, call_id, "haan", llm)
    r3 = await _turn(memory, call_id, "busy hoon, baad mein call karo", llm)

    # The callback flow's utter is spoken verbatim.
    assert r3.reply_id == "plo_obj_callback_pd", r3.reply_id
    assert "व्यस्त" in (r3.reply_text or "") or "busy" in (r3.reply_text or "").lower()


# ---------------------------------------------------------------------------
# DEBT-031: bare-yes at identity slot = confirmed (Devanagari tokenization fix)
# ---------------------------------------------------------------------------


def test_debt031_bare_haan_confirms_identity():
    """Bare "haan" at plo_identity_response → coerce_identity → confirmed."""
    profile = get_tenant_profile("paisalo")
    cmds = coerce_identity([], "plo_identity_response", "haan", profile=profile)
    assert any(
        c.command == "set_slot" and c.name == "plo_identity_response" and c.value == "confirmed"
        for c in cmds
    )


def test_debt031_bare_devanagari_haan_ji_confirms_identity():
    """Bare "हाँ जी। हाँ।" (the live failing transcript) → confirmed.

    Regression for the live CALL 1 Session B T2 bug: ASR produced Devanagari
    "हाँ जी। हाँ।" but ``re.findall(r"\\w+", ...)`` split the syllables into base
    consonants (ह / ज) because Devanagari matras + candrabindu are not ``\\w`` in
    Python's ``re``. The token intersection with ``id_yes_tokens`` (हाँ / हां / जी)
    was empty, so coercion fell through to clarify and the bot re-asked identity.
    """
    profile = get_tenant_profile("paisalo")
    cmds = coerce_identity([], "plo_identity_response", "हाँ जी। हाँ।", profile=profile)
    assert any(
        c.command == "set_slot" and c.name == "plo_identity_response" and c.value == "confirmed"
        for c in cmds
    ), "bare Devanagari haan-ji must confirm identity (DEBT-031)"


def test_debt031_haan_main_ramesh_confirms_identity():
    """\"haan, main Ramesh bol raha hoon\" → confirmed (token + phrase paths)."""
    profile = get_tenant_profile("paisalo")
    cmds = coerce_identity(
        [], "plo_identity_response", "haan, main Ramesh bol raha hoon", profile=profile
    )
    assert any(
        c.command == "set_slot" and c.name == "plo_identity_response" and c.value == "confirmed"
        for c in cmds
    )


def test_debt031_bare_haan_devanagari_confirms_identity():
    """Bare Devanagari "हाँ" alone → confirmed."""
    profile = get_tenant_profile("paisalo")
    cmds = coerce_identity([], "plo_identity_response", "हाँ", profile=profile)
    assert any(
        c.command == "set_slot" and c.name == "plo_identity_response" and c.value == "confirmed"
        for c in cmds
    )


def test_debt031_bare_ji_devanagari_confirms_identity():
    """Bare Devanagari "जी" alone → confirmed."""
    profile = get_tenant_profile("paisalo")
    cmds = coerce_identity([], "plo_identity_response", "जी", profile=profile)
    assert any(
        c.command == "set_slot" and c.name == "plo_identity_response" and c.value == "confirmed"
        for c in cmds
    )


def test_debt031_nahi_denies_identity():
    """\"nahi\" at identity slot → denied (not confirmed)."""
    profile = get_tenant_profile("paisalo")
    cmds = coerce_identity([], "plo_identity_response", "nahi", profile=profile)
    assert any(
        c.command == "set_slot" and c.name == "plo_identity_response" and c.value == "denied"
        for c in cmds
    )


def test_debt031_no_yes_token_falls_through():
    """A non-yes/non-no transcript at identity slot → no coercion (fall through)."""
    profile = get_tenant_profile("paisalo")
    cmds = coerce_identity([], "plo_identity_response", "office kahan se", profile=profile)
    assert not any(c.command == "set_slot" and c.name == "plo_identity_response" for c in cmds)
