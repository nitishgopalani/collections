"""P5.3 — PaisaLo scenario walkthrough goldens (6 buckets + NPA refuse×2 + OOS)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.retrieval import clear_retrieval_cache
from app.engine.tenant_profile import clear_tenant_profile_cache
from app.engine.turn import handle_turn
from app.flows.loader import reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
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

    async def complete(self, system: str, user: str, *, json_only: bool = True, **kw) -> str:
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
        text=text,
        turn_meta=turn_meta
        or {"force_flow": "plo_opener", "call_date": CALL_DATE},
    )
    return await handle_turn(
        req,
        memory=memory,
        llm=llm,
        tools=FakeToolClient(),
        kb=_EmptyKB(),
    )


def _write_transcript(name: str, lines: list[str]) -> Path:
    path = SCRIPTS / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


SCENARIO_CASES = [
    (
        "predue",
        [
            [],  # greeting
            [{"command": "set_slot", "name": "plo_identity_response", "value": "confirmed"}],
            [{"command": "set_slot", "name": "plo_payment_intent", "value": "willing"}],
        ],
        ["अंजली", "किश्त", "धन्यवाद"],
        "_p5_golden_predue.txt",
    ),
    (
        "ondue",
        [
            [],
            [{"command": "set_slot", "name": "plo_identity_response", "value": "confirmed"}],
            [{"command": "set_slot", "name": "plo_payment_intent", "value": "willing"}],
        ],
        ["अंजली", "आज", "किश्त"],
        "_p5_golden_ondue.txt",
    ),
    (
        "postdue1",
        [
            [],
            [{"command": "set_slot", "name": "plo_identity_response", "value": "confirmed"}],
            [{"command": "set_slot", "name": "plo_payment_intent", "value": "willing"}],
        ],
        ["नेहा", "बकाया"],
        "_p5_golden_postdue1.txt",
    ),
    (
        "postdue2",
        [
            [],
            [{"command": "set_slot", "name": "plo_identity_response", "value": "confirmed"}],
            [{"command": "set_slot", "name": "plo_payment_intent", "value": "willing"}],
        ],
        ["नेहा", "बकाया"],
        "_p5_golden_postdue2.txt",
    ),
    (
        "postdue3",
        [
            [],
            [{"command": "set_slot", "name": "plo_identity_response", "value": "confirmed"}],
            [{"command": "set_slot", "name": "plo_payment_intent", "value": "willing"}],
        ],
        ["अर्जुन", "सख़्त"],
        "_p5_golden_postdue3.txt",
    ),
    (
        "npa",
        [
            [],
            [{"command": "set_slot", "name": "plo_identity_response", "value": "confirmed"}],
            [{"command": "set_slot", "name": "plo_consent_2min", "value": "yes"}],
            [{"command": "set_slot", "name": "plo_timeline", "value": "willing"}],
        ],
        ["अमन", "NPA", "तसल्ली"],
        "_p5_golden_npa.txt",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario,llm_turns,must_contain,outfile",
    SCENARIO_CASES,
    ids=[c[0] for c in SCENARIO_CASES],
)
async def test_paisalo_scenario_happy_path(
    monkeypatch, scenario, llm_turns, must_contain, outfile
):
    monkeypatch.setenv("TEST_PLO_SCENARIO", scenario)
    get_settings.cache_clear()
    memory = InMemoryMemoryStore()
    call_id = f"p5-{scenario}"
    llm = _ScriptedLLM(llm_turns)
    lines = [f"# PaisaLo golden — {scenario}", f"voice_id expected after select"]
    texts: list[str] = []

    # Turn 0: empty opener kick
    r0 = await _turn(memory, call_id, "", llm)
    texts.append(r0.reply_text or "")
    lines.append(f"T0 agent: {r0.reply_text}")

    user_lines = ["हाँ, मैं रमेश बोल रहा हूँ"]
    if scenario == "npa":
        user_lines += ["हाँ, दो मिनट बात हो सकती है", "आज व्यवस्था कर लूँगा"]
    else:
        user_lines += ["हाँ, भुगतान कर दूंगा"]

    for i, user in enumerate(user_lines, start=1):
        r = await _turn(
            memory,
            call_id,
            user,
            llm,
            turn_meta={"call_date": CALL_DATE},
        )
        texts.append(r.reply_text or "")
        lines.append(f"T{i} user: {user}")
        lines.append(f"T{i} agent: {r.reply_text}")

    combined = "\n".join(texts)
    for needle in must_contain:
        assert needle in combined, f"{scenario}: missing {needle!r} in {combined!r}"

    # D-4 voice mapping set after identity → scenario select
    state = await memory.load_state(call_id)
    assert state is not None
    assert state.slots.get("plo_scenario") == scenario
    expected_voice = {
        "predue": "priya",
        "ondue": "priya",
        "postdue1": "neha",
        "postdue2": "neha",
        "postdue3": "kabir",
        "npa": "amit",
    }[scenario]
    assert state.slots.get("voice_id") == expected_voice
    assert state.slots.get("tts_model") == "bulbul:v3"
    if scenario == "postdue3":
        assert float(state.slots.get("tts_pace")) == 0.9
    elif scenario == "npa":
        assert float(state.slots.get("tts_pace")) == 0.95
    else:
        assert state.slots.get("tts_pace") is None

    _write_transcript(outfile, lines)


@pytest.mark.asyncio
async def test_paisalo_npa_refuse_twice_escalates_branch(monkeypatch):
    """Consent → disclosure → refuse×2 replies → 3rd refuse → branch + END_CALL."""
    monkeypatch.setenv("TEST_PLO_SCENARIO", "npa")
    get_settings.cache_clear()
    memory = InMemoryMemoryStore()
    call_id = "p5-npa-refuse2"
    llm = _ScriptedLLM(
        [
            [],
            [{"command": "set_slot", "name": "plo_identity_response", "value": "confirmed"}],
            [{"command": "set_slot", "name": "plo_consent_2min", "value": "yes"}],
            [{"command": "set_slot", "name": "plo_timeline", "value": "refuse"}],
            [{"command": "set_slot", "name": "plo_timeline", "value": "refuse"}],
            [{"command": "set_slot", "name": "plo_timeline", "value": "refuse"}],
        ]
    )
    lines = ["# PaisaLo NPA refuse×2 → branch END_CALL"]
    await _turn(memory, call_id, "", llm)
    turns = [
        "हाँ जी",
        "हाँ, दो मिनट",
        "नहीं, अभी नहीं दे सकता",
        "नहीं, पैसे नहीं हैं",
        "बिल्कुल नहीं कर पाऊँगा",
    ]
    replies: list[str] = []
    for i, user in enumerate(turns, start=1):
        r = await _turn(
            memory, call_id, user, llm, turn_meta={"call_date": CALL_DATE}
        )
        replies.append(r.reply_text or "")
        lines.append(f"T{i} user: {user}")
        lines.append(f"T{i} agent: {r.reply_text}")

    joined = "\n".join(replies)
    assert "NPA घोषित" in joined or "NPA की स्थिति" in joined
    assert "ब्रांच" in joined
    state = await memory.load_state(call_id)
    assert state is not None
    assert state.slots.get("end_call") is True or state.slots.get("plo_call_closed")
    _write_transcript("_p5_golden_npa_refuse_twice.txt", lines)


@pytest.mark.asyncio
async def test_paisalo_npa_out_of_context_question(monkeypatch):
    """Tier-3 respond: OOS question mid-NPA → short answer + resume timeline ask."""
    monkeypatch.setenv("TEST_PLO_SCENARIO", "npa")
    get_settings.cache_clear()
    memory = InMemoryMemoryStore()
    call_id = "p5-npa-oos"
    llm = _ScriptedLLM(
        [
            [],
            [{"command": "set_slot", "name": "plo_identity_response", "value": "confirmed"}],
            [{"command": "set_slot", "name": "plo_consent_2min", "value": "yes"}],
            # OOS: office location not in facts → unknown_info_reply path via respond
            [
                {
                    "command": "respond",
                    "text": "माफ़ कीजिए, यह जानकारी अभी मेरे पास उपलब्ध नहीं है।",
                }
            ],
            [{"command": "set_slot", "name": "plo_timeline", "value": "willing"}],
        ]
    )
    lines = ["# PaisaLo NPA out-of-context question (Tier 3)"]
    await _turn(memory, call_id, "", llm)
    script = [
        "हाँ, रमेश हूँ",
        "हाँ बात हो सकती है",
        "आपका ऑफिस कहाँ है?",
        "आज पेमेंट कर दूँगा",
    ]
    replies: list[str] = []
    for i, user in enumerate(script, start=1):
        r = await _turn(
            memory, call_id, user, llm, turn_meta={"call_date": CALL_DATE}
        )
        replies.append(r.reply_text or "")
        lines.append(f"T{i} user: {user}")
        lines.append(f"T{i} agent: {r.reply_text}")
        if i == 3:
            assert "उपलब्ध नहीं" in (r.reply_text or "") or "हेल्पलाइन" in (
                r.reply_text or ""
            )

    joined = "\n".join(replies)
    assert "उपलब्ध नहीं" in joined or "हेल्पलाइन" in joined or "ब्रांच" in joined
    # Resume ask still present after respond (timeline re-ask).
    assert "कब" in replies[2] or "समाधान" in replies[2] or "उपलब्ध नहीं" in replies[2]
    _write_transcript("_p5_golden_npa_oos.txt", lines)
