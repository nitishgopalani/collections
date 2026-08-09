"""W1-C C2 — DNC / opt-out capture (policy interrupt).

Verifies the policy-lane DNC detector fires BEFORE the Tier-1 evidence
scorer on cues by which the caller asks us to stop calling (``dobara call
mat karna`` / ``call mat karo`` / ``pareshan mat karo`` …), preempts the
turn, speaks the non-committal ``policy_stop_calls`` wording (request
recorded; final confirmation from the brand — does NOT promise dialer
suppression until W4 dialer work), tags ``disposition=dnc_requested``, and
graceful ENDs (outcome 7). The scorer never runs on a DNC cue.
"""

from __future__ import annotations

import pytest

from app.clients.tools_sim import FakeToolClient
from app.config import get_settings, tenant_config
from app.engine.retrieval import clear_retrieval_cache
from app.engine.safety import dnc_preempt
from app.engine.tenant_profile import clear_tenant_profile_cache
from app.engine.tracker import apply, new_conversation_state
from app.engine.turn import handle_turn
from app.flows.loader import reload_flow_set
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest
from app.schemas.command import Command

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


class _CountingLLM:
    def __init__(self):
        self.call_count = 0

    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def complete(self, system: str, user: str, *, json_only: bool = True, **kw) -> str:
        self.call_count += 1
        return "[]"


async def _turn(memory, call_id, text, llm):
    req = TurnRequest(
        call_id=call_id,
        borrower_id="plo_dnc_borrower",
        tenant_id="paisalo",
        channel="voice",
        locale="hi-IN",
        transcript=text,
        turn_meta={"force_flow": "plo_opener", "call_date": CALL_DATE},
    )
    return await handle_turn(
        req,
        memory=memory,
        llm=llm,
        tools=FakeToolClient(),
        kb=_EmptyKB(),
    )


@pytest.mark.parametrize(
    "cue",
    [
        "dobara call mat karna",
        "call mat karo please",
        "pareshan mat karo baar baar",
        "baar baar mat call karo",
        "ab se call mat karna",
        "do not call me again",
        "stop calling me",
    ],
)
@pytest.mark.asyncio
async def test_c2_dnc_cue_preempts_before_tier1_tags_disposition_and_ends(cue):
    memory = InMemoryMemoryStore()
    llm = _CountingLLM()
    response = await _turn(memory, "call-dnc-1", cue, llm)

    # Policy-lane preemption: scorer never ran.
    assert llm.call_count == 0, (
        f"evidence scorer ran ({llm.call_count} LLM calls) for DNC cue; "
        "dnc_preempt must preempt BEFORE Tier-1"
    )
    # Named disposition + graceful END (outcome 7).
    assert response.disposition == "dnc_requested", (
        f"disposition={response.disposition!r}, want dnc_requested for cue: {cue}"
    )
    assert response.end_call is True, "DNC must graceful END (end_call=True)"
    # Non-committal ack: request recorded, final confirmation from brand.
    assert response.reply_text.strip()
    assert "request" in response.reply_text.lower() or "रिक्वेस्ट" in response.reply_text or "darj" in response.reply_text.lower(), (
        f"reply not non-committal ack: {response.reply_text!r}"
    )
    # Must NOT promise suppression (that's W4 dialer work).
    lower = response.reply_text.lower()
    assert "aage contact nahi" not in lower, (
        f"DNC reply promises suppression (W4 work): {response.reply_text!r}"
    )
    # No flow was started (preempted before candidate selection).
    assert response.actions_executed == []


@pytest.mark.asyncio
async def test_c2_dnc_does_not_suppress_dunning_at_dialer_level():
    """The DNC flag is recorded for audit but dunning_suppressed is NOT set —
    dialer suppression is W4 work and promising it now would be a lie."""
    state = new_conversation_state("c-dnc-state", "paisalo", "plo_dnc_borrower")
    state = apply(state, [Command(command="start_flow", flow="hardship")])
    cfg = tenant_config("paisalo")
    result = dnc_preempt("dobara call mat karna", state, cfg)
    assert result is not None
    assert result.end_call is True
    assert result.compliance_updates.get("dnc_requested") is True
    # Critical: do NOT promise dialer suppression until W4.
    assert not result.compliance_updates.get("dunning_suppressed"), (
        "dnc_preempt must not set dunning_suppressed — that is W4 dialer work"
    )


def test_c2_paisalo_config_carries_dnc_cue_pack_and_non_committal_reply():
    cfg = tenant_config("paisalo")
    assert any("mat kar" in s for s in cfg.dnc_signals), cfg.dnc_signals
    assert any("pareshan" in s for s in cfg.dnc_signals), cfg.dnc_signals
    # Non-committal reply distinct from opt_out_ack_reply (which promises suppression).
    assert cfg.policy_stop_calls_reply.strip()
    assert cfg.policy_stop_calls_reply != cfg.opt_out_ack_reply, (
        "policy_stop_calls_reply must be the non-committal wording, not opt_out_ack_reply"
    )
