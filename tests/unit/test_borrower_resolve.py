"""Borrower resolution on session_start."""

from unittest.mock import AsyncMock

import pytest

from app.schemas.state import BorrowerRecord
from app.ws.borrower_resolve import resolve_session_borrower
from app.ws.session import BrainWSSession


@pytest.mark.asyncio
async def test_resolve_session_borrower_by_phone():
    memory = AsyncMock()
    memory.lookup_borrower_by_phone = AsyncMock(
        return_value=BorrowerRecord(
            borrower_id="B_RAJESH",
            identity={"name": "Rajesh"},
            loan={"amount_due": 350},
            comms_prefs={"phone": "+919876543210"},
        )
    )
    memory.load_borrower = AsyncMock(return_value=None)
    memory.save_borrower = AsyncMock()

    session = BrainWSSession(
        session_id="s1",
        borrower_id="unknown",
        agent_id="agent-1",
        pack_id="",
        locale="hi-IN",
        tenant_id="default",
        force_flow=None,
        borrower_context={"phone": "+919876543210", "borrower_name": "Rajesh"},
        started=True,
    )
    record = await resolve_session_borrower(memory, session)
    assert record is not None
    assert session.borrower_id == "B_RAJESH"
    assert record.identity["name"] == "Rajesh"
    memory.save_borrower.assert_awaited_once()
