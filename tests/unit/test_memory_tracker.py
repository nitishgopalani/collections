import pytest

from app.engine.tracker import apply, hydrate_from_borrower, new_conversation_state
from app.exceptions import StaleStateError
from app.memory.store import InMemoryMemoryStore
from app.schemas.command import Command
from app.schemas.state import BorrowerRecord, Event


@pytest.fixture
def memory_store() -> InMemoryMemoryStore:
    return InMemoryMemoryStore()


@pytest.mark.asyncio
async def test_state_roundtrip(memory_store: InMemoryMemoryStore):
    state = new_conversation_state("call-1", "default", "borrower-1")
    state = apply(state, [Command(command="set_slot", name="dpd", value=30)])
    await memory_store.save_state(state)

    loaded = await memory_store.load_state("call-1")
    assert loaded is not None
    assert loaded.version == 1
    assert loaded.slots["dpd"] == 30


@pytest.mark.asyncio
async def test_stale_version_raises(memory_store: InMemoryMemoryStore):
    state = new_conversation_state("call-stale", "default", "borrower-1")
    state = apply(state, [Command(command="clarify")])
    await memory_store.save_state(state)

    stale = state.model_copy(deep=True)
    stale.version = 1
    with pytest.raises(StaleStateError):
        await memory_store.save_state(stale)


@pytest.mark.asyncio
async def test_audit_append_ordered(memory_store: InMemoryMemoryStore):
    borrower_id = "borrower-audit"
    for idx in range(3):
        await memory_store.append_audit(
            Event(ts=f"t{idx}", kind="test", data={"seq": idx}),
            call_id=f"call-{idx}",
            borrower_id=borrower_id,
        )

    entries = await memory_store.list_audit(borrower_id)
    assert len(entries) == 3
    # immutable append-only: re-append does not replace
    for idx, raw in enumerate(entries):
        assert f'"seq": {idx}' in raw or f'"seq":{idx}' in raw.replace(" ", "")


@pytest.mark.asyncio
async def test_borrower_persists(memory_store: InMemoryMemoryStore):
    record = BorrowerRecord(
        borrower_id="borrower-persist",
        loan={"amount_due": 5000, "dpd": 12, "bucket": "B1"},
        compliance_flags={"vulnerable": False},
    )
    await memory_store.save_borrower(record)
    loaded = await memory_store.load_borrower("borrower-persist")
    assert loaded is not None
    assert loaded.loan["amount_due"] == 5000


def test_hydrate_from_borrower():
    state = new_conversation_state("c", "t", "b")
    borrower = BorrowerRecord(
        borrower_id="b",
        loan={"outstanding": 9000, "dpd": 5, "bucket": "B0"},
        compliance_flags={"dispute_hold": True},
    )
    hydrated = hydrate_from_borrower(state, borrower)
    assert hydrated.slots["amount_due"] == 9000
    assert hydrated.slots["dpd"] == 5
    assert hydrated.slots["bucket"] == "B0"
    assert hydrated.slots["compliance_flags"]["dispute_hold"] is True


def test_apply_start_flow_and_set_slot():
    state = new_conversation_state("c", "t", "b")
    state = apply(
        state,
        [
            Command(command="start_flow", flow="promise_to_pay"),
            Command(command="set_slot", name="ptp_date", value="2026-06-27"),
        ],
    )
    assert state.version == 1
    assert len(state.flow_stack) == 1
    assert state.flow_stack[0].flow == "promise_to_pay"
    assert state.slots["ptp_date"] == "2026-06-27"
    assert len(state.events) == 2


def test_apply_cancel_flow():
    state = new_conversation_state("c", "t", "b")
    state = apply(state, [Command(command="start_flow", flow="dispute")])
    state = apply(state, [Command(command="cancel_flow", flow="dispute")])
    assert state.flow_stack == []
    assert state.version == 2
