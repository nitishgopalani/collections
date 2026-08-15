"""W3-3 post-call obligation loop.

Replay L1-L4 + the PTP beyond-policy call → exact rows in
dispositions / callbacks / worklist. Worklist carries a 30-word snippet (R2).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings
from app.engine.obligation_export import (
    RECORD_FIELDS,
    read_jsonl,
    reset_webhook_stub,
    stub_emitted,
    transcript_snippet,
)
from app.engine.retrieval import clear_retrieval_cache
from app.engine.tenant_profile import clear_tenant_profile_cache
from app.engine.turn import handle_turn
from app.flows.loader import reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest

CALL_DATE = "2026-08-15"
TODAY = date(2026, 8, 15)
STAMP = "20260815"
TENANT = "paisalo"
PTP_10D = "2026-08-25"
PTP_FAR = "2026-09-29"  # 45d, W3-1 accept_flagged


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("SCRIPTED_CATALOG_ROUTING", "true")
    monkeypatch.setenv("CALL_WINDOW_START", "00:00")
    monkeypatch.setenv("CALL_WINDOW_END", "23:59")
    monkeypatch.setenv("COMMITMENT_GATE_ENFORCE", "true")
    monkeypatch.setenv("EXPORTS_DIR", str(tmp_path))
    monkeypatch.setenv("TEST_PLO_SCENARIO", "postdue3")
    clear_tenant_profile_cache()
    get_settings.cache_clear()
    reload_flow_set()
    clear_retrieval_cache()
    reset_webhook_stub()
    yield
    reset_webhook_stub()
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
    def __init__(self, turns=None):
        self._responses = [json.dumps(t, ensure_ascii=False) for t in (turns or [])]
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


def _req(call_id: str, text: str, borrower_id: str) -> TurnRequest:
    return TurnRequest(
        call_id=call_id,
        borrower_id=borrower_id,
        tenant_id=TENANT,
        channel="voice",
        locale="hi-IN",
        transcript=text,
        turn_meta={"force_flow": "plo_opener", "call_date": CALL_DATE},
    )


async def _say(memory, call_id, text, llm, borrower_id: str | None = None):
    return await handle_turn(
        _req(call_id, text, borrower_id or call_id),
        memory=memory,
        llm=llm,
        tools=FakeToolClient(),
        kb=_EmptyKB(),
    )


def _set_scenario(monkeypatch, scenario: str) -> None:
    monkeypatch.setenv("TEST_PLO_SCENARIO", scenario)
    get_settings.cache_clear()


async def _close_with_date(memory, call_id, llm, *, npa: bool = False):
    await _say(memory, call_id, "", llm)
    await _say(memory, call_id, "हाँ, मैं रमेश बोल रहा हूँ।", llm)
    if npa:
        await _say(memory, call_id, "हाँ, दो मिनट बात हो सकती है", llm)
    await _say(memory, call_id, "हाँ, 10 दिन बाद भेजूँगा।", llm)
    return await _say(memory, call_id, "हाँ पक्का।", llm)


# ---------------------------------------------------------------------------
# Pure
# ---------------------------------------------------------------------------


def test_snippet_caps_at_30_words():
    words = " ".join(f"w{i}" for i in range(40))
    snip = transcript_snippet(words, 30)
    assert len(snip.split()) == 30
    assert snip.startswith("w0")
    assert "w29" in snip
    assert "w30" not in snip


# ---------------------------------------------------------------------------
# Ladder L1-L4 + PTP live + callback + flagged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ladder_l1_l4_and_ptp_export_exact_rows(monkeypatch, tmp_path):
    root = Path(tmp_path)
    memory = InMemoryMemoryStore()

    _set_scenario(monkeypatch, "ondue")
    await _close_with_date(memory, "l1-ondue", _ScriptedLLM())

    _set_scenario(monkeypatch, "postdue1")
    await _close_with_date(memory, "l2-pd1", _ScriptedLLM())

    _set_scenario(monkeypatch, "postdue3")
    await _close_with_date(memory, "l3-pd3", _ScriptedLLM())

    _set_scenario(monkeypatch, "npa")
    await _close_with_date(memory, "l4-npa", _ScriptedLLM(), npa=True)

    _set_scenario(monkeypatch, "postdue3")
    llm = _ScriptedLLM()
    await _say(memory, "ptp-live", "", llm)
    await _say(memory, "ptp-live", "हाँ, मैं रमेश बोल रहा हूँ।", llm)
    await _say(memory, "ptp-live", "मैं 45 दिन बाद दूंगा।", llm)
    await _say(memory, "ptp-live", "हाँ पक्का।", llm)
    await _say(memory, "ptp-live", "नहीं, 45 दिन बाद ही।", llm)

    _set_scenario(monkeypatch, "postdue3")
    await _say(memory, "w33-callback", "", _ScriptedLLM())
    await _say(memory, "w33-callback", "हाँ, मैं रमेश बोल रहा हूँ।", _ScriptedLLM())
    await _say(memory, "w33-callback", "baad mein call karo", _ScriptedLLM())

    dnc_text = "dobara call mat karna please stop calling us now"
    await _say(memory, "w33-dnc", "", _ScriptedLLM())
    await _say(memory, "w33-dnc", dnc_text, _ScriptedLLM())

    disp = {r["session_id"]: r for r in read_jsonl(root / f"dispositions_{STAMP}.jsonl")}
    assert set(disp) == {
        "l1-ondue",
        "l2-pd1",
        "l3-pd3",
        "l4-npa",
        "ptp-live",
        "w33-callback",
        "w33-dnc",
    }
    for sid, scenario in (
        ("l1-ondue", "ondue"),
        ("l2-pd1", "postdue1"),
        ("l3-pd3", "postdue3"),
        ("l4-npa", "npa"),
    ):
        row = disp[sid]
        assert row["disposition"] == "PTP_SET"
        assert row["scenario"] == scenario
        assert row["borrower_id"] == sid
        assert row["tenant"] == TENANT
        assert row["ptp_date"] == PTP_10D
        assert row["ptp_amount"] == 4500
        assert isinstance(row["flags"], list)
        assert isinstance(row["duration"], int)
        assert row["call_ts"]

    live = disp["ptp-live"]
    assert live["disposition"] == "PTP_SET"
    assert live["ptp_date"] == PTP_FAR
    assert "ptp_beyond_policy" in live["flags"]

    assert disp["w33-callback"]["disposition"] == "callback_request"
    assert disp["w33-dnc"]["disposition"] == "dnc_requested"

    csv_path = root / f"dispositions_{STAMP}.csv"
    assert csv_path.is_file()
    header = csv_path.read_text(encoding="utf-8").splitlines()[0]
    for col in RECORD_FIELDS:
        assert col in header

    callbacks = {r["session_id"]: r for r in read_jsonl(root / f"callbacks_{STAMP}.jsonl")}
    assert set(callbacks) == {"w33-callback"}
    assert callbacks["w33-callback"]["disposition"] == "callback_request"

    worklist = {r["session_id"]: r for r in read_jsonl(root / f"worklist_{STAMP}.jsonl")}
    assert set(worklist) == {"w33-dnc"}
    snip = worklist["w33-dnc"]["snippet"]
    assert "dobara" in snip
    assert len(snip.split()) <= 30
    assert worklist["w33-dnc"]["disposition"] == "dnc_requested"

    emitted_ids = {r["session_id"] for r in stub_emitted()}
    assert "l3-pd3" in emitted_ids
    assert "ptp-live" in emitted_ids
