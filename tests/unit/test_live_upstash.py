import os
import uuid

import pytest
from dotenv import load_dotenv

from app.config import get_settings
from app.engine.tracker import apply, hydrate_from_borrower, new_conversation_state
from app.exceptions import StaleStateError
from app.memory.audit import parse_audit_records
from app.memory.store import (
    UpstashMemoryStore,
    borrower_key,
    create_memory_store,
    state_key,
)
from app.schemas.command import Command
from app.schemas.state import BorrowerRecord, Event


def _live_upstash_enabled() -> bool:
    load_dotenv()
    get_settings.cache_clear()
    settings = get_settings()
    return (
        not settings.memory_stub_mode
        and settings.upstash_redis_rest_url
        and settings.upstash_redis_rest_token
    )


@pytest.fixture
async def live_store():
    load_dotenv()
    os.environ["STUB_MODE"] = "false"
    os.environ.setdefault("TOOLS_STUB", "true")
    get_settings.cache_clear()
    store = create_memory_store()
    if not isinstance(store, UpstashMemoryStore):
        pytest.skip("Upstash store not configured")
    if not await store.ping():
        pytest.skip("Upstash not reachable")
    suffix = uuid.uuid4().hex[:8]
    yield store, suffix
    await store.delete_keys(
        state_key(f"live-call-{suffix}"),
        borrower_key(f"live-borrower-{suffix}"),
        f"audit:live-borrower-{suffix}",
    )
    get_settings.cache_clear()


@pytest.mark.live_upstash
@pytest.mark.asyncio
async def test_live_state_roundtrip(live_store):
    store, suffix = live_store
    call_id = f"live-call-{suffix}"
    state = new_conversation_state(call_id, "default", f"live-borrower-{suffix}")
    state = apply(state, [Command(command="set_slot", name="dpd", value=21)])
    await store.save_state(state)

    loaded = await store.load_state(call_id)
    assert loaded is not None
    assert loaded.slots["dpd"] == 21
    assert loaded.version == 1


@pytest.mark.live_upstash
@pytest.mark.asyncio
async def test_live_stale_version_raises(live_store):
    store, suffix = live_store
    call_id = f"live-call-{suffix}"
    state = new_conversation_state(call_id, "default", f"live-borrower-{suffix}")
    state = apply(state, [Command(command="clarify")])
    await store.save_state(state)

    stale = state.model_copy(deep=True)
    with pytest.raises(StaleStateError):
        await store.save_state(stale)


@pytest.mark.live_upstash
@pytest.mark.asyncio
async def test_live_audit_ordered_immutable(live_store):
    store, suffix = live_store
    borrower_id = f"live-borrower-{suffix}"
    kinds = ["first", "second", "third"]
    for kind in kinds:
        await store.append_audit(
            Event(ts="t", kind=kind, data={}),
            call_id=f"call-{suffix}",
            borrower_id=borrower_id,
        )

    raw = await store.list_audit(borrower_id)
    records = parse_audit_records(raw)
    assert [r.event.kind for r in records] == kinds


@pytest.mark.live_upstash
@pytest.mark.asyncio
async def test_live_borrower_persists_across_load(live_store):
    store, suffix = live_store
    borrower_id = f"live-borrower-{suffix}"
    record = BorrowerRecord(
        borrower_id=borrower_id,
        loan={"amount_due": 7500, "dpd": 8, "bucket": "B2"},
        compliance_flags={"opt_out": False},
    )
    await store.save_borrower(record)

    state = new_conversation_state(f"call-a-{suffix}", "default", borrower_id)
    loaded_borrower = await store.load_borrower(borrower_id)
    state = hydrate_from_borrower(state, loaded_borrower)
    assert state.slots["amount_due"] == 7500
    assert state.slots["dpd"] == 8


@pytest.mark.skipif(not _live_upstash_enabled(), reason="Set STUB_MODE=false with Upstash creds")
@pytest.mark.live_upstash
def test_live_upstash_config_gate():
    assert _live_upstash_enabled()
