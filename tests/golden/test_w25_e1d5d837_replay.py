"""L1-FIX F1-F6 + full e1d5d837 replay golden.

Live (26b3af2, 15 Aug ~12:58 IST) left the 4-probe script:
  t2 "बोल रही थी।" → D1 false identity (बोल रह ⊂ bot echo), not echo HOLD
  t4/t5 "आप कौन बोल रहे हैं?" → invented compose ids → unknown_info
  t6/t7 set_slot text=refused/no rejected → clarify
  t8 refusal → willing-shaped confirm_plo_payment_intent
  t9 D2 replay of t7 broken JSON → confirm loop

Expected after F1-F6:
  t2 echo HOLD; t4 compose[fact_caller_identity]; t6 ONE refusal-confirm;
  t7 "नहीं..." locks refused and push/close proceeds; no confirm loop.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.catalog import infer_scenario_key
from app.engine.command_gen import (
    COMPOSE_FEW_SHOTS,
    parse_and_validate_commands,
    parse_validate_success,
)
from app.engine.commitment_gate import commitment_gate
from app.engine.echo_filter import detect_echo
from app.engine.evidence_scorer import confirms_pending_value, score_evidence
from app.engine.fragment_library import build_fragment_index, get_fragment
from app.engine.retrieval import clear_retrieval_cache
from app.engine.robustness import PENDING_CONFIRM_KEY
from app.engine.scripted_coercions import (
    UNWILLINGNESS_RE,
    coerce_payment_refusal,
    cue_hit_pack,
)
from app.engine.tenant_profile import clear_tenant_profile_cache, get_tenant_profile
from app.engine.tracker import new_conversation_state
from app.engine.turn import handle_turn
from app.flows.loader import get_flow_set, reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.schemas.command import Command

CALL_DATE = "2026-08-15"
TENANT = "paisalo"
OPENER = (
    "नमस्ते, मैं अंजली पैसालो से बोल रही हूँ। "
    "क्या मेरी बात रमेश जी से हो रही है?"
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("SCRIPTED_CATALOG_ROUTING", "true")
    monkeypatch.setenv("CALL_WINDOW_START", "00:00")
    monkeypatch.setenv("CALL_WINDOW_END", "23:59")
    monkeypatch.setenv("TEST_PLO_SCENARIO", "ondue")
    monkeypatch.setenv("COMMITMENT_GATE_ENFORCE", "true")
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
        self._responses = [json.dumps(t, ensure_ascii=False) for t in turns]
        self.call_count = 0
        self.last_system = ""
        self.last_user = ""

    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def complete(self, system: str, user: str, *, json_only: bool = True, **kw) -> str:
        self.call_count += 1
        self.last_system = system
        self.last_user = user
        if self.call_count <= len(self._responses):
            return self._responses[self.call_count - 1]
        return "[]"


def _req(call_id: str, text: str) -> TurnRequest:
    return TurnRequest(
        call_id=call_id,
        borrower_id="plo_test_borrower",
        tenant_id=TENANT,
        channel="voice",
        locale="hi-IN",
        transcript=text,
        turn_meta={"force_flow": "plo_opener", "call_date": CALL_DATE},
    )


def _guards(caplog, needle: str) -> dict:
    rows = [
        r.getMessage()
        for r in caplog.records
        if "turn_decision" in r.getMessage() and needle in r.getMessage()
    ]
    assert rows, f"turn_decision missing needle={needle!r}"
    msg = rows[-1]
    start = msg.find("{")
    return json.loads(msg[start:])


def _inner(guards: dict) -> dict:
    return guards.get("guards") or guards


def _who_compose():
    return [{
        "command": "compose",
        "fragments": ["fact_caller_identity"],
        "oof_class": "call_context",
    }]


# ---------------------------------------------------------------------------
# F1 fragment index + real ids
# ---------------------------------------------------------------------------


def test_f1_fragment_index_includes_fact_caller_identity():
    idx = build_fragment_index(TENANT, "ondue")
    ids = {row["id"] for row in idx}
    assert "fact_caller_identity" in ids
    row = next(r for r in idx if r["id"] == "fact_caller_identity")
    assert "who_are_you" in row["answers"]
    assert "who_are_you" not in ids
    assert "fact_agent_intro" not in ids
    assert "fact_caller_identity" in COMPOSE_FEW_SHOTS


def test_f1_index_scenario_drops_postdue_only_facts():
    ondue = {r["id"] for r in build_fragment_index(TENANT, "ondue")}
    postdue = {r["id"] for r in build_fragment_index(TENANT, "postdue")}
    assert "fact_dpd" not in ondue
    assert "fact_dpd" in postdue


@pytest.mark.asyncio
async def test_f1_aap_kaun_compose_fact_caller_identity(caplog):
    memory = InMemoryMemoryStore()
    call_id = "f1-kaun"
    llm = _ScriptedLLM([_who_compose()])
    with caplog.at_level(logging.INFO, logger="app.engine.turn_decision_log"):
        await handle_turn(
            _req(call_id, ""), memory=memory, llm=llm,
            tools=FakeToolClient(), kb=_EmptyKB(),
        )
        await handle_turn(
            _req(call_id, "हाँ, मैं रमेश बोल रहा हूँ।"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
        t3 = await handle_turn(
            _req(call_id, "aap kaun bol rahe hain"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
    assert "fact_caller_identity" in llm.last_user
    assert "who_are_you" in llm.last_user
    assert t3.reply_text
    assert "unknown_info" not in (t3.reply_text or "")
    assert any(tok in t3.reply_text for tok in ("अंजली", "पैसालो", "ब्रांच"))
    guards = _inner(_guards(caplog, "aap kaun"))
    assert guards.get("compose_fired") is True
    assert "fact_caller_identity" in (guards.get("compose_fragment_ids") or [])


# ---------------------------------------------------------------------------
# F2 text→value alias
# ---------------------------------------------------------------------------


def test_f2_set_slot_text_aliases_to_value():
    raw = json.dumps([{"command": "set_slot", "name": "plo_payment_intent", "text": "refused"}])
    result = parse_and_validate_commands(raw)
    assert result.commands[0].command == "set_slot"
    assert result.commands[0].value == "refused"
    assert "text->value" in result.alias_used
    assert parse_validate_success(result) is True


# ---------------------------------------------------------------------------
# F3 identity skip + echo
# ---------------------------------------------------------------------------


def test_f3_id_yes_phrases_drop_bot_utterance_substrings():
    profile = get_tenant_profile(TENANT)
    phrases = profile.cues("id_yes_phrases")
    assert "बोल रह" not in phrases
    assert "bol raha" not in phrases
    assert "bol rahi" not in phrases


def test_f3_identity_cue_skip_bare_yes_or_yes_name_only():
    profile = get_tenant_profile(TENANT)
    assert cue_hit_pack(
        "हाँ", "plo_identity_response", profile=profile, on_rails=True,
        borrower_name="रमेश",
    ) == "identity"
    assert cue_hit_pack(
        "हाँ, मैं रमेश बोल रहा हूँ।", "plo_identity_response",
        profile=profile, on_rails=True, borrower_name="रमेश",
    ) == "identity"
    assert cue_hit_pack(
        "बोल रही थी।", "plo_identity_response",
        profile=profile, on_rails=True, borrower_name="रमेश",
    ) is None


def test_f3_t2_class_echo_holds():
    assert detect_echo("बोल रही थी।", OPENER) is True


# ---------------------------------------------------------------------------
# F4 unwilling vs inability
# ---------------------------------------------------------------------------


def test_f4_unwillingness_forms_tag_unwilling():
    profile = get_tenant_profile(TENANT)
    assert UNWILLINGNESS_RE.search("नहीं, मैं नहीं करूँगा।")
    cmds, fired, via, cls = coerce_payment_refusal(
        [], "plo_payment_intent", "नहीं, मैं नहीं करूँगा।", profile=profile
    )
    assert fired is True
    assert cls == "unwilling"
    assert any(c.value == "refused" for c in cmds)
    assert cue_hit_pack(
        "नहीं, मैं नहीं करूँगा।", "plo_payment_intent",
        profile=profile, on_rails=True,
    ) == "refusal"


def test_f4_inability_still_tagged_inability():
    profile = get_tenant_profile(TENANT)
    _cmds, fired, _via, cls = coerce_payment_refusal(
        [], "plo_payment_intent", "आज नहीं चल पाएगी", profile=profile
    )
    assert fired is True
    assert cls == "inability"


# ---------------------------------------------------------------------------
# F5 value-aware confirm + same-v evidence 3
# ---------------------------------------------------------------------------


def test_f5_refused_picks_refusal_confirm_fragment():
    assert get_fragment(TENANT, "confirm_plo_payment_intent_refused")
    verdict = commitment_gate(
        [Command(command="set_slot", name="plo_payment_intent", value="refused")],
        evidence={"evidence": 2, "evidence_reason": "cue_agree"},
        cost_table=None,
        slot_cost_class={"plo_payment_intent": "money_state"},
        identity_ok=True,
        awaited_slot="plo_payment_intent",
    )
    assert verdict["verdict"] == "downgrade"
    assert verdict["confirm_fragment_id"] == "confirm_plo_payment_intent_refused"


def test_f5_pending_refused_plus_nahi_is_evidence_3():
    profile = get_tenant_profile(TENANT)
    assert confirms_pending_value("नहीं।", profile, "refused") is True
    state = new_conversation_state("f5", TENANT, "b")
    score = score_evidence(
        transcript="नहीं।",
        state=state,
        profile=profile,
        llm_calls=0, commands=[], last_spoken_reply="",
        echo=False, awaited_slot="plo_payment_intent",
        pending_confirm=True, pending_value="refused",
    )
    assert score["evidence"] == 3
    assert score["evidence_reason"] == "explicit_confirm"


# ---------------------------------------------------------------------------
# F6 D2 write-through only on parse success
# ---------------------------------------------------------------------------


def test_f6_rejected_parse_is_not_success():
    raw = json.dumps([{"command": "set_slot", "name": "plo_payment_intent"}])
    result = parse_and_validate_commands(raw)
    assert result.rejections
    assert parse_validate_success(result) is False
    clarify = parse_and_validate_commands("not-json")
    assert parse_validate_success(clarify) is False


# ---------------------------------------------------------------------------
# Full e1d5d837 replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e1d5d837_full_replay(caplog):
    memory = InMemoryMemoryStore()
    call_id = "e1d5d837-replay"
    llm = _ScriptedLLM([_who_compose(), _who_compose()])
    confirm_ids: list[str] = []

    with caplog.at_level(logging.INFO, logger="app.engine.turn_decision_log"):
        t1 = await handle_turn(
            _req(call_id, ""), memory=memory, llm=llm,
            tools=FakeToolClient(), kb=_EmptyKB(),
        )
        t2 = await handle_turn(
            _req(call_id, "बोल रही थी।"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
        t3 = await handle_turn(
            _req(call_id, "हाँ, मैं रमेश बोल रहा हूँ।"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
        t4 = await handle_turn(
            _req(call_id, "आप कौन बोल रहे हैं?"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
        t5 = await handle_turn(
            _req(call_id, "आप बोल कौन रहे हैं?"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
        t6 = await handle_turn(
            _req(call_id, "नहीं, मैं नहीं करूँगा।"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
        state_t6 = await memory.load_state(call_id)
        t7 = await handle_turn(
            _req(call_id, "नहीं।"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
        t8 = await handle_turn(
            _req(call_id, "मैंने बताया ना नहीं कर पाऊंगा।"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
        t9 = await handle_turn(
            _req(call_id, "नहीं।"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )
        t10 = await handle_turn(
            _req(call_id, "नहीं, मैं भुगतान नहीं कर पाऊँगा।"),
            memory=memory, llm=llm, tools=FakeToolClient(), kb=_EmptyKB(),
        )

    assert t1.reply_text
    # t2: echo HOLD (empty reply, no identity write)
    assert getattr(t2, "disposition", "") == "ECHO_HOLD" or t2.reply_text == ""
    # t3 identity → ondue detail
    assert t3.reply_text
    assert any(tok in t3.reply_text for tok in ("किश्त", "देय", "भुगतान"))
    # t4/t5: real fragment, not unknown_info
    assert t4.reply_text
    assert any(tok in t4.reply_text for tok in ("अंजली", "पैसालो"))
    assert "unknown_info" not in t4.reply_text
    assert t5.reply_text
    assert "unknown_info" not in t5.reply_text

    # t6: ONE refusal-confirm, slot not yet written
    assert t6.reply_text
    assert "नहीं करेंगे" in t6.reply_text or "सही" in t6.reply_text
    assert "तैयार" not in t6.reply_text
    assert state_t6 is not None
    assert state_t6.slots.get("plo_payment_intent") != "refused"
    pending = state_t6.slots.get(PENDING_CONFIRM_KEY)
    assert isinstance(pending, dict)
    assert pending.get("value") == "refused"

    # t7: नहीं confirms refusal → push/close, no second willing-confirm
    assert t7.reply_text
    assert "तैयार" not in (t7.reply_text or "")
    state = await memory.load_state(call_id)
    assert state is not None
    assert state.slots.get("plo_payment_intent") == "refused"

    decisions = []
    for rec in caplog.records:
        msg = rec.getMessage()
        if "turn_decision" not in msg:
            continue
        start = msg.find("{")
        if start < 0:
            continue
        try:
            decisions.append(json.loads(msg[start:]))
        except json.JSONDecodeError:
            continue
    t6_row = next(
        d for d in decisions
        if "नहीं करूँगा" in str(d.get("transcript") or "")
    )
    t6g = _inner(t6_row)
    confirm_ids.append(t6g.get("confirm_fragment_id"))
    assert t6g.get("gate_verdict") == "downgrade"
    assert t6g.get("confirm_fragment_id") == "confirm_plo_payment_intent_refused"

    nahi_rows = [
        d for d in decisions
        if str(d.get("transcript") or "").strip() in {"नहीं।", "नहीं"}
    ]
    assert nahi_rows, "t7 नहीं turn_decision missing"
    t7g = _inner(nahi_rows[0])
    assert t7g.get("evidence") == 3
    assert t7g.get("gate_verdict") == "execute"

    # t8-t10: no confirm loop (no second refusal-confirm, no clarify spiral)
    later = " ".join(
        filter(None, [t7.reply_text, t8.reply_text, t9.reply_text, t10.reply_text])
    )
    assert later.count("नहीं करेंगे") <= 0
    assert "clarify" not in later.lower()
    # Refusal flow proceeded (push and/or close script)
    assert any(tok in later for tok in ("अंतिम", "क्रेडिट", "जल्द", "धन्यवाद", "देरी"))

    assert t8.reply_text is not None
    assert t9.reply_text is not None
    assert t10.reply_text is not None
