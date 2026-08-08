"""FIX-B regression: TEST_MODE fixture fires ONLY when TEST_PLO_SCENARIO env is set.

When unset, the DB borrower wins; if DB has nothing, proceed as unknown
borrower (no silent Ramesh fixture fallback). When set (e.g. npa), the fixture
overrides regardless of DB.
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.engine.retrieval import clear_retrieval_cache
from app.engine.tenant_profile import clear_tenant_profile_cache
from app.engine.turn import handle_turn
from app.flows.loader import reload_flow_set
from app.schemas.api import TurnRequest
from app.schemas.state import BorrowerRecord


class _FakeMemory:
    """In-memory store with a DB-backed lookup_borrower_by_phone."""

    def __init__(self, *, db_borrower: BorrowerRecord | None = None) -> None:
        self._state: dict[str, object] = {}
        self._borrowers: dict[str, BorrowerRecord] = {}
        self._db_borrower = db_borrower
        self.lookup_calls = 0

    async def load_state(self, call_id: str):
        return self._state.get(call_id)

    async def save_state(self, state) -> None:
        self._state[state.call_id] = state

    async def load_borrower(self, borrower_id: str) -> BorrowerRecord | None:
        return self._borrowers.get(borrower_id)

    async def save_borrower(self, record: BorrowerRecord) -> None:
        self._borrowers[record.borrower_id] = record.model_copy(deep=True)

    async def lookup_borrower_by_phone(self, phone: str, *, tenant_id: str = "default"):
        self.lookup_calls += 1
        return self._db_borrower

    async def append_audit(self, event, *, call_id, borrower_id, tenant_id="") -> str:
        return "audit-stub"


class _NoOpLLM:
    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def complete(self, system: str, user: str, *, json_only: bool = True, **kw) -> str:
        return "[]"


class _EmptyKB:
    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def retrieve(self, text, tenant_id, k: int = 6):
        return []


def _seeded_predue_borrower() -> BorrowerRecord:
    return BorrowerRecord(
        borrower_id="PLO_RAMESH_PREDUE",
        identity={"name": "Ramesh"},
        loan={
            "customer_name": "Ramesh",
            "repay_amount": 4500,
            "loan_amount": 50000,
            "due_date": "2026-08-13",
            "disbursal_date": "2026-02-09",
            "amount_due": 4500,
            "days_past_due": -5,
            "dpd": -5,
            "branch": "Kanpur City",
            "branch_address": "12 MG Road, Kanpur",
            "last_date_paid": "2026-07-13",
            "product": "ABF",
            "npa_flag": False,
        },
        comms_prefs={"phone": "+919810587857", "language": "hi-IN"},
    )


def _req(call_id: str, *, borrower_id: str = "unknown", phone: str = "+919810587857") -> TurnRequest:
    return TurnRequest(
        call_id=call_id,
        tenant_id="paisalo",
        borrower_id=borrower_id,
        transcript="",
        turn_meta={
            "opener": True,
            "force_flow": "plo_opener",
            "borrower_context": {"phone": phone, "borrower_phone": phone},
        },
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("TEST_TENANT_ID", "paisalo")
    # Ensure no fixture override by default.
    monkeypatch.delenv("TEST_PLO_SCENARIO", raising=False)
    clear_tenant_profile_cache()
    get_settings.cache_clear()
    reload_flow_set()
    clear_retrieval_cache()
    yield
    clear_retrieval_cache()
    clear_tenant_profile_cache()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_seeded_ani_no_env_db_wins_predue_priya():
    """(1) Seeded ANI + TEST_PLO_SCENARIO unset → DB row wins → predue/priya, dpd=-5."""
    db = _seeded_predue_borrower()
    memory = _FakeMemory(db_borrower=db)
    resp = await handle_turn(
        _req("fixb-1"), memory=memory, kb=_EmptyKB(), llm=_NoOpLLM(), tools=None
    )
    state = await memory.load_state("fixb-1")
    assert state.slots.get("plo_scenario") == "predue"
    assert state.slots.get("voice_id") == "priya"
    assert state.slots.get("tts_model") == "bulbul:v3"
    assert int(state.slots.get("dpd")) == -5
    assert state.slots.get("customer_name") == "Ramesh"
    assert state.slots.get("branch_address") == "12 MG Road, Kanpur"
    # Greeting is the predue PDF greeting (Anjali), spoken in priya — no amit.
    assert "अंजली" in (resp.reply_text or "")
    assert memory.lookup_calls == 1


@pytest.mark.asyncio
async def test_env_npa_overrides_db_to_npa_amit(monkeypatch):
    """(2) TEST_PLO_SCENARIO=npa → fixture overrides DB → npa/amit regardless of DB row."""
    monkeypatch.setenv("TEST_PLO_SCENARIO", "npa")
    get_settings.cache_clear()
    db = _seeded_predue_borrower()  # DB says dpd=-5 (predue), but env forces npa.
    memory = _FakeMemory(db_borrower=db)
    await handle_turn(
        _req("fixb-2"), memory=memory, kb=_EmptyKB(), llm=_NoOpLLM(), tools=None
    )
    state = await memory.load_state("fixb-2")
    assert state.slots.get("plo_scenario") == "npa"
    assert state.slots.get("voice_id") == "amit"
    assert state.slots.get("tts_model") == "bulbul:v3"


@pytest.mark.asyncio
async def test_unseeded_ani_no_env_unknown_borrower_not_ramesh():
    """(3) Unseeded ANI + TEST_PLO_SCENARIO unset → unknown borrower, NOT Ramesh fixture."""
    memory = _FakeMemory(db_borrower=None)  # no DB row for this phone
    await handle_turn(
        _req("fixb-3", phone="+919999999999"),
        memory=memory,
        kb=_EmptyKB(),
        llm=_NoOpLLM(),
        tools=None,
    )
    state = await memory.load_state("fixb-3")
    # No fixture fallback: borrower stays unknown, no Ramesh, no dpd=15/postdue1.
    assert state.slots.get("customer_name") != "रमेश"
    assert state.slots.get("customer_name") != "Ramesh"
    assert state.slots.get("plo_scenario") != "postdue1"
    assert int(state.slots.get("dpd") or 0) != 15
    assert memory.lookup_calls == 1


# ---------------------------------------------------------------------------
# FIX-A: voice before first utterance — priya from turn 1, no amit→X switch.
# ---------------------------------------------------------------------------

class _ScriptedLLM:
    """LLM that confirms identity, then answers payment intent."""

    def __init__(self, turns: list[list[dict]]) -> None:
        self._turns = list(turns)
        self.i = 0

    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def complete(self, system, user, *, json_only: bool = True, **kw) -> str:
        import json
        if self.i < len(self._turns):
            cmds = self._turns[self.i]
            self.i += 1
            return json.dumps(cmds)
        return "[]"


@pytest.mark.asyncio
async def test_fixa_predue_priya_from_turn1_no_switch():
    """FIX-A: seeded predue row → first utterance is priya (not amit), greeting
    matches the predue PDF greeting (Anjali), and voice_id stays priya across the
    whole session (no amit→X switch)."""
    db = _seeded_predue_borrower()
    memory = _FakeMemory(db_borrower=db)
    llm = _ScriptedLLM(
        [
            # T0: opener kick — LLM returns no commands; flow walks the opener.
            [],
            # T1: confirm identity
            [{"command": "set_slot", "name": "plo_identity_response", "value": "confirmed"}],
            # T2: willing to pay
            [{"command": "set_slot", "name": "plo_payment_intent", "value": "willing"}],
        ]
    )
    # T0: opener kick
    r0 = await handle_turn(_req("fixa-1"), memory=memory, kb=_EmptyKB(), llm=llm, tools=None)
    s0 = await memory.load_state("fixa-1")
    assert s0.slots.get("voice_id") == "priya", "voice_id must be priya from turn 1"
    assert s0.slots.get("tts_model") == "bulbul:v3"
    # Greeting is the predue PDF greeting (Anjali persona, spoken by priya).
    assert "अंजली" in (r0.reply_text or ""), "greeting must name Anjali (predue PDF)"
    assert "किश्त" in (r0.reply_text or ""), "greeting must mention the installment (predue PDF)"
    # No amit anywhere in the opener slots.
    assert s0.slots.get("voice_id") != "amit"

    # T1: confirm identity → routes to plo_predue (wait_intent).
    r1 = await handle_turn(
        TurnRequest(
            call_id="fixa-1",
            tenant_id="paisalo",
            borrower_id=s0.borrower_id,
            transcript="हाँ रमेश बोल रहा हूँ",
            turn_meta={"borrower_context": {"phone": "+919810587857", "borrower_phone": "+919810587857"}},
        ),
        memory=memory,
        kb=_EmptyKB(),
        llm=llm,
        tools=None,
    )
    s1 = await memory.load_state("fixa-1")
    assert s1.slots.get("voice_id") == "priya", "voice_id must stay priya after identity"

    # T2: willing → ack. Voice must still be priya (no switch).
    r2 = await handle_turn(
        TurnRequest(
            call_id="fixa-1",
            tenant_id="paisalo",
            borrower_id=s0.borrower_id,
            transcript="हाँ भुगतान कर दूँगा",
            turn_meta={"borrower_context": {"phone": "+919810587857", "borrower_phone": "+919810587857"}},
        ),
        memory=memory,
        kb=_EmptyKB(),
        llm=llm,
        tools=None,
    )
    s2 = await memory.load_state("fixa-1")
    assert s2.slots.get("voice_id") == "priya", "voice_id must stay priya through the session"
    # Ack closes the call.
    assert "धन्यवाद" in (r2.reply_text or "")
