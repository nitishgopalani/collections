"""Replay a console fixture through handle_turn and compare expect vs actual."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.clients.tools_sim import FakeToolClient
from app.engine.tenant_profile import get_tenant_profile
from app.engine.turn import handle_turn
from app.memory.store import InMemoryMemoryStore
from app.schemas.api import TurnRequest

CALL_DATE = "2026-08-15"
FIXTURES_DIR = Path(__file__).resolve().parent / "console"


class EmptyKB:
    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def retrieve(self, text, tenant_id, k: int = 6):
        return []


class StubLLM:
    def __init__(self) -> None:
        self.call_count = 0

    @property
    def is_stub(self) -> bool:
        return False

    async def ping(self) -> bool:
        return True

    async def complete(self, system, user, *, json_only=True, **kw) -> str:
        self.call_count += 1
        return "[]"


def load_fixture(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _class_match(reply_id: str | None, reply_class: str) -> bool:
    rid = (reply_id or "").lower()
    cls = (reply_class or "").lower().strip()
    if not cls:
        return True
    tokens = {
        "greet": ("_greet", "opener_identity"),
        "greeting": ("_greeting",),
        "which_emi": ("which_emi",),
        "refuse": ("_refuse",),
        "push": ("_push",),
        "wrong_number": ("wrong_number",),
        "callback": ("callback",),
        "already_paid": ("already_paid",),
        "reask": ("reask", "identity_ask"),
        "confirm": ("confirm_",),
        "assurance": ("_ack", "_assurance"),
        "consent": ("consent",),
        "disclosure": ("disclosure",),
        "dead_air": ("repair_dead_air", "unknown"),
    }
    needles = tokens.get(cls, (cls,))
    return any(n in rid for n in needles)


def diff_expect(expect: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    rows: list[str] = []
    if "reply_id" in expect and expect["reply_id"] not in (None, ""):
        if actual.get("reply_id") != expect["reply_id"]:
            rows.append(
                f"reply_id  expected {expect['reply_id']!r}  " f"actual {actual.get('reply_id')!r}"
            )
    if expect.get("reply_id_any"):
        allowed = list(expect["reply_id_any"])
        if actual.get("reply_id") not in allowed:
            rows.append(f"reply_id  expected one of {allowed}  actual {actual.get('reply_id')!r}")
    if expect.get("reply_class"):
        if not _class_match(actual.get("reply_id"), str(expect["reply_class"])):
            rows.append(
                f"reply_class  expected {expect['reply_class']!r}  "
                f"actual {actual.get('reply_id')!r}"
            )
    if "reply_empty" in expect:
        empty = not str(actual.get("reply_text") or "").strip()
        if bool(expect["reply_empty"]) != empty:
            rows.append(f"reply_empty  expected {expect['reply_empty']!r}  actual {empty}")
    for key in ("evidence_reason", "gate_verdict", "oof_class", "disposition"):
        if key in expect and expect[key] not in (None, ""):
            if actual.get(key) != expect[key]:
                rows.append(f"{key}  expected {expect[key]!r}  actual {actual.get(key)!r}")
    if expect.get("compose_fragments"):
        wanted = [str(x) for x in expect["compose_fragments"]]
        got = [str(x) for x in (actual.get("compose_fragment_ids") or [])]
        if any(w not in got for w in wanted):
            rows.append(f"compose_fragments  expected {wanted} in {got}")
    if "complaint_raised" in expect:
        if bool(actual.get("complaint_raised")) != bool(expect["complaint_raised"]):
            rows.append(
                f"complaint_raised  expected {expect['complaint_raised']!r}  "
                f"actual {actual.get('complaint_raised')!r}"
            )
    if "payment_claimed" in expect:
        if bool(actual.get("payment_claimed")) != bool(expect["payment_claimed"]):
            rows.append(
                f"payment_claimed  expected {expect['payment_claimed']!r}  "
                f"actual {actual.get('payment_claimed')!r}"
            )
    return rows


async def replay_fixture(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    tenant = str(fixture.get("tenant_id") or "paisalo")
    borrower = str(fixture.get("borrower_id") or "plo_test_borrower")
    call_id = str(fixture.get("id") or fixture.get("session_id") or "fixture")
    memory = InMemoryMemoryStore()
    llm = StubLLM()
    tools = FakeToolClient()
    kb = EmptyKB()
    out: list[dict[str, Any]] = []
    for i, turn in enumerate(fixture.get("turns") or []):
        transcript = str(turn.get("transcript") or "")
        scenario = str(fixture.get("scenario") or turn.get("scenario") or "postdue1")
        prof = get_tenant_profile(tenant)
        meta: dict[str, Any] = {
            "force_flow": f"{(prof.flow_prefix if prof else 'plo_')}opener",
            "call_date": CALL_DATE,
            "scenario_override": scenario,
            "scenario_override_slot": (
                (prof.test_scenario_override_slot if prof else "") or "plo_scenario_override"
            ),
        }
        req = TurnRequest(
            call_id=call_id,
            borrower_id=borrower,
            tenant_id=tenant,
            channel="voice",
            locale="hi-IN",
            transcript=transcript,
            turn_meta=meta,
        )
        result = await handle_turn(req, memory=memory, llm=llm, tools=tools, kb=kb)
        state = await memory.load_state(call_id)
        guards = dict((state.slots.get("_last_guards") if state else None) or {})
        actual = {
            "turn": i,
            "transcript": transcript,
            "reply_id": result.reply_id,
            "reply_text": result.reply_text,
            "disposition": result.disposition or guards.get("disposition"),
            "evidence_reason": guards.get("evidence_reason"),
            "gate_verdict": guards.get("gate_verdict"),
            "oof_class": guards.get("oof_class"),
            "compose_fragment_ids": list(
                guards.get("compose_fragment_ids") or guards.get("fragment_ids") or []
            ),
            "complaint_raised": bool(
                guards.get("complaint_raised")
                or (state.slots.get("complaint_raised") if state else False)
            ),
            "payment_claimed": bool(state.slots.get("payment_claimed") if state else False),
        }
        expect = dict(turn.get("expect") or {})
        actual["diffs"] = diff_expect(expect, actual)
        out.append(actual)
    return out
