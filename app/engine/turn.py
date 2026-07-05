"""Single-turn orchestration — full pipeline (Sprint 7)."""

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import UTC, date, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from app.config import get_settings, tenant_config
from app.engine.actions import make_async_action_runner
from app.engine.command_gen import generate
from app.engine.compliance_handoff import sync_compliance_notes_on_persist
from app.engine.dispute_breadth import sync_dispute_on_persist
from app.engine.executor import ExecResult
from app.engine.executor import run_async as run_executor_async
from app.engine.followup import hydrate_followup_from_borrower, sync_followup_on_persist
from app.engine.gate import gate
from app.engine.hardship import sync_hardships_on_persist
from app.engine.identity_gate import apply_identity_entry_gate, defer_collection_flows
from app.engine.label_transition import run_label_transition
from app.engine.latency import StageTimer, TurnLatencyProfile
from app.engine.nlg import ResolvedReply, draft_reply_resolved
from app.engine.priority import reorder
from app.engine.refusal_negotiation import sync_refusal_negotiation_on_persist
from app.engine.retrieval import retrieve_flow_candidates
from app.engine.robustness import (
    FRUSTRATION_COUNT_KEY,
    FRUSTRATION_ESCALATION_DISPOSITION,
    mark_repair_escalation,
    record_outbound_context,
    track_frustration,
    track_slot_reask,
)
from app.engine.slot_validation import validate_commands
from app.clients.whatsapp import send_whatsapp
from app.engine.safety import apply_safety_to_state, safety_preempt
from app.engine.tracker import apply, hydrate_from_borrower, new_conversation_state
from app.engines_p2.decision_overlay import apply_decision_overlay
from app.engines_p2.emotion import (
    apply_emotion_to_state,
    classify_emotion_from_turn,
    sync_emotion_on_persist,
)
from app.engines_p2.persona import apply_persona_to_state, sync_persona_on_persist
from app.engines_p2.recovery_prob import apply_recovery_to_state, sync_recovery_on_persist
from app.engines_p2.risk import apply_risk_to_state, sync_risk_on_persist
from app.engines_p2.trust import apply_trust_to_state, sync_trust_on_persist
from app.flows.loader import get_flow_set
from app.flows.manifest import MANIFEST_VERSION, load_reply_manifest
from app.flows.override_provider import NullOverrideProvider, OverrideProvider
from app.flows.overrides import OverrideValidationError, merge_response_overrides
from app.memory.audit import TurnAuditChain, build_turn_audit_record
from app.schemas.api import TurnRequest, TurnResponse
from app.schemas.command import Command
from app.schemas.flow import FlowSet
from app.schemas.manifest import ReplyManifest
from app.schemas.overrides import BrandOverridePack
from app.schemas.state import BorrowerRecord, ConversationState, Event, Frame
from app.ws.borrower_context import (
    apply_borrower_context_to_record,
    apply_borrower_context_to_state,
    normalize_borrower_context,
)
from app.ws.routing import FORCE_FLOW_ALIASES
from app.telemetry import annotate_turn_span, span, turn_trace
from app.engine.turn_decision_log import log_turn_decision

logger = logging.getLogger(__name__)

# Strong refs to detached transfer/whatsapp tasks so the loop can't GC them mid-flight.
_TRANSFER_TASKS: set[asyncio.Task[Any]] = set()
_WHATSAPP_TASKS: set[asyncio.Task[Any]] = set()


async def _send_whatsapp_bg(*, phone: str, name: str) -> None:
    """Fire the templated WhatsApp send detached from the turn (never raises)."""
    try:
        await send_whatsapp(phone=phone, name=name)
    except Exception:  # noqa: BLE001 — detached task must never surface an error
        logger.exception("whatsapp send failed name=%s", name)


# Warm transfer driver: poll cadence for GET /v1/transfer/{id}.
_TRANSFER_POLL_S = 1.0


async def _drive_warm_transfer(
    hold_s: float,
    *,
    session_uuid: str,
    target: str,
    caller_id: str,
    reason: str,
    answer_budget_s: float,
    complete_delay_s: float,
) -> None:
    """Run a full warm transfer against the ari-orchestrator. Never raises.

    Detached from the turn (background task) so the handoff line's TTS is sent
    immediately. Sequence:

    1. hold — let the "connecting you to a senior" line play before the agent
       can answer into the three-way;
    2. POST /v1/transfer by session_uuid — the orchestrator dials the agent;
       on answer the agent joins the customer's existing bridge (three-way
       with the AI, which stays up: its death would tear the whole call down);
    3. poll status until ``up`` / terminal / the answer budget runs out;
    4. ``up``      -> transfer/complete: the AI leg is dropped, customer and
       agent stay bridged (the connector session ends, the brain session dies
       with it — nothing more for us to do);
       no answer   -> transfer/cancel, then hang up the customer leg: the flow
       already closed (handoff line played, script over), so ending the call
       matches the legacy TRANSFER_FAILED behaviour;
       ``failed``  -> agent busy/declined: same hangup fallback;
       ``finished``/``cancelled`` -> the customer hung up mid-ring (the
       orchestrator already cleaned up) — nothing to do.
    """
    from app.clients import orchestrator

    try:
        if hold_s > 0:
            await asyncio.sleep(hold_s)
        out = await asyncio.to_thread(
            orchestrator.warm_transfer,
            session_uuid=session_uuid,
            transfer_to=target,
            caller_id=caller_id,
        )
        transfer_id = str(out.get("transfer_id") or "")
        if not transfer_id:
            logger.error(
                "warm transfer: no transfer_id session=%s response=%s", session_uuid, out
            )
            return
        logger.info(
            "warm transfer started session=%s transfer_id=%s target=%s reason=%s",
            session_uuid,
            transfer_id,
            target,
            reason,
        )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(answer_budget_s, _TRANSFER_POLL_S)
        status, st = "", {}
        while loop.time() < deadline:
            await asyncio.sleep(_TRANSFER_POLL_S)
            st = await asyncio.to_thread(
                orchestrator.transfer_status, transfer_id=transfer_id
            )
            status = str(st.get("status") or "")
            if status in ("up", "failed", "finished", "cancelled", "completed"):
                break

        if status == "up":
            if complete_delay_s > 0:
                await asyncio.sleep(complete_delay_s)
            await asyncio.to_thread(
                orchestrator.transfer_complete, transfer_id=transfer_id
            )
            logger.info(
                "warm transfer completed session=%s transfer_id=%s (AI leg dropped)",
                session_uuid,
                transfer_id,
            )
            return
        if status in ("finished", "cancelled", "completed"):
            logger.info(
                "warm transfer already terminal session=%s transfer_id=%s status=%s",
                session_uuid,
                transfer_id,
                status,
            )
            return

        # No answer within budget (or busy/declined). Cancel if still ringing,
        # then end the call: the flow already spoke the handoff line and closed
        # the script, so there is nothing left for the AI to say.
        if status != "failed":
            await asyncio.to_thread(
                orchestrator.transfer_cancel, transfer_id=transfer_id
            )
        customer = str(st.get("customer_channel_id") or "")
        logger.warning(
            "warm transfer failed session=%s transfer_id=%s status=%s "
            "(hanging up customer leg %s)",
            session_uuid,
            transfer_id,
            status or "no-answer",
            customer or "?",
        )
        if customer:
            await asyncio.to_thread(orchestrator.hangup, channel_id=customer)
    except Exception:  # noqa: BLE001 — detached task must never surface an error
        logger.exception("warm transfer driver failed session=%s", session_uuid)

_REPLY_MANIFEST: ReplyManifest = load_reply_manifest()

# Salary_on_time: while collecting one of these answers, a refusal/timing reply is
# the expected slot value — not a reason to launch a deflection objection script.
SOT_COMMIT_COLLECT_SLOTS: frozenset[str] = frozenset(
    {
        "sot_payment_intent",
        "sot_payment_intent_2",
        "sot_payment_intent_3",
        "sot_payment_intent_4",
        "sot_payment_intent_5",
        "sot_commit_timing",
        "sot_customer_time",
        "sot_ondue_decision",
        "sot_afterdue_decision",
        "sot_final_confirm",
    }
)
SOT_DEFLECTION_OBJECTIONS: frozenset[str] = frozenset(
    {
        "sot_obj_busy",
        "sot_obj_hold",
        "sot_obj_wont_pay",
        "sot_obj_pay_later_today",
        "sot_obj_no_timeline",
        "sot_obj_out_of_station",
    }
)
# While the borrower is inside the identity/push/commit journey, their reply is the
# awaited answer (a name/yes-no, a reason, an intent, a time) — not a trigger to jump
# into an objection script. Suppress objections for the whole journey, not just the
# final collect step. sot_opener is included so a plain identity reply can't derail
# into sot_obj_is_bot / sot_obj_company at the greeting.
SOT_ONRAILS_FLOWS: frozenset[str] = frozenset(
    {
        "sot_opener",
        "sot_push",
        "sot_commit",
        "sotod_offer",
        "sotod_push",
        "sotpd_offer",
        "sotpd_push",
    }
)
# salary_on_time has no live human queue / cannot-handle path, so these commands
# only stall the flow (the LLM was emitting human_handoff on plain "haan"/"theek hai").
SOT_BLOCKED_COMMANDS: frozenset[str] = frozenset({"human_handoff", "cannot_handle"})


def _awaiting_collect_slot(state: ConversationState, flows: FlowSet) -> str:
    """Slot the active (paused) flow step is waiting to collect, or "" if none."""
    if not state.flow_stack:
        return ""
    frame = state.flow_stack[-1]
    flow = flows.flows.get(frame.flow)
    if flow is None or frame.step_index >= len(flow.steps):
        return ""
    return flow.steps[frame.step_index].collect or ""


# Negation cues that flip a re-stated timing at the confirm step from "yes" to "no"
# (a change of plan). Kept conservative: Hindi tag "na" ("kar dunga na" = yes) is
# NOT a negation, so it is deliberately excluded.
_SOT_NEGATION_CUES: tuple[str, ...] = (
    "nahi", "nahin", "nhi", "mat ", "नहीं", "मत", "ना करूं", "नही",
)


def _coerce_sot_confirm(
    commands: list[Command], awaiting_slot: str, transcript: str
) -> list[Command]:
    """Resolve a re-stated time/day into yes/no at the final-confirm step.

    Per the script, once we've captured the payment time and ask "yeh confirm hai?",
    the borrower re-stating the same time ("haan parso shaam tak", "6 baje tak") IS a
    confirmation. But Groq's non-strict JSON sometimes writes sot_customer_time /
    sot_commit_timing again instead of sot_final_confirm — which never fills the
    collect slot, so the flow loops re-asking the time. When we're waiting on the
    confirm and the LLM only re-stated the timing, resolve it here: a negated reply
    ("aaj NAHI, parso karunga") is a change → 'no' (re-open timing); otherwise 'yes'.
    """
    if awaiting_slot != "sot_final_confirm":
        return commands
    if any(c.command == "set_slot" and c.name == "sot_final_confirm" for c in commands):
        return commands
    restated = any(
        c.command == "set_slot"
        and c.name in {"sot_customer_time", "sot_commit_timing"}
        for c in commands
    )
    if not restated:
        return commands
    low = (transcript or "").lower()
    value = "no" if any(cue in low for cue in _SOT_NEGATION_CUES) else "yes"
    return [Command(command="set_slot", name="sot_final_confirm", value=value)]


# Bare yes/no cues used to resolve the identity confirmation when the LLM returns a
# clarify instead of setting sot_identity_response. Short tokens are matched on word
# boundaries (so "ji" doesn't hit inside "raji"); phrases are matched as substrings.
# ASCII short cues are matched on word boundaries (so "ji" doesn't fire inside
# "raji"); Devanagari cues are matched as substrings because \w in Python's re does
# not include combining vowel signs (so "जी" would tokenize to just "ज").
_SOT_ID_YES_TOKENS: frozenset[str] = frozenset(
    {
        "haan", "haa", "han", "hanji", "ji", "jee", "yes", "yep", "yup", "yeah",
        "bilkul", "sahi", "correct",
    }
)
_SOT_ID_YES_PHRASES: tuple[str, ...] = (
    "haan ji", "ji haan", "ji han", "main hi", "main hoon", "mai hoon", "mai hu",
    "main bol", "mai bol", "bol raha", "bol rahi", "speaking", "wahi hu", "wahi hoon",
    "हाँ", "हां", "जी", "बोल रह", "मैं ही", "मैं हू", "मैं हो",
)
_SOT_ID_NO_TOKENS: frozenset[str] = frozenset({"nahi", "nahin", "nhi", "no"})
_SOT_ID_NO_PHRASES: tuple[str, ...] = (
    "galat number", "wrong number", "wrong person", "nahi hu", "nahi hoon",
    "koi aur", "नहीं", "नही", "गलत नंबर", "कोई और",
)


def _coerce_sot_identity(
    commands: list[Command], awaiting_slot: str, transcript: str
) -> list[Command]:
    """Resolve a bare yes/no into sot_identity_response at the identity step.

    The opener asks "kya main <name> ji se baat kar raha hoon?". A lone "haan"/"ji"/
    "yes" IS a confirmation, but the LLM sometimes returns a clarify (no set_slot),
    which routes to retry_identity and re-greets forever. When we're waiting on
    sot_identity_response and the LLM didn't set it, map a bare affirmation ->
    confirmed and a bare wrong-number/negation -> denied. Anything that states a
    relation is left to the LLM (it maps to 'relation').
    """
    if awaiting_slot != "sot_identity_response":
        return commands
    if any(
        c.command == "set_slot" and c.name == "sot_identity_response" for c in commands
    ):
        return commands
    low = (transcript or "").strip().lower()
    if not low:
        return commands
    tokens = set(re.findall(r"\w+", low, flags=re.UNICODE))
    if any(p in low for p in _SOT_ID_NO_PHRASES) or (tokens & _SOT_ID_NO_TOKENS):
        return [
            Command(command="set_slot", name="sot_identity_response", value="denied")
        ]
    if any(p in low for p in _SOT_ID_YES_PHRASES) or (tokens & _SOT_ID_YES_TOKENS):
        return [
            Command(command="set_slot", name="sot_identity_response", value="confirmed")
        ]
    return commands


# "Did you get the payment link?" negation cues -> not_received. Everything else
# (affirmation, unclear, silence) -> received, so the link flow always resolves and
# closes rather than looping on the collect.
_SOT_LINK_NOT_RECEIVED_CUES: tuple[str, ...] = (
    "nahi mila", "nahin mila", "nhi mila", "nahi aaya", "nahin aaya", "nahi aya",
    "nahi aa raha", "abhi tak nahi", "abhi nahi aaya", "kuch nahi aaya",
    "koi link nahi", "link nahi mila", "link nahi aaya", "link nahi",
    "not received", "didnt get", "didn't get", "did not get", "not yet", "no link",
    "नहीं मिला", "नहीं आया", "अभी तक नहीं", "अभी नहीं आया", "कोई लिंक नहीं", "लिंक नहीं",
)


def _coerce_sot_link_received(
    commands: list[Command], awaiting_slot: str, transcript: str
) -> list[Command]:
    """Resolve the borrower's reply at the link-receipt check into sot_link_received.

    After the link-request flow sends the link it asks whether it arrived. A negation
    ("abhi tak nahi mila") routes to the re-send + reassurance branch; anything else
    (affirmation, unclear, or silence) routes to the graceful thank-and-close branch.
    Guarantees the slot is always set while awaiting it, so the flow never loops.

    Authoritative: the LLM tends to answer this yes/no question with boolean-style
    values ("true"/"false") that do not match the flow's ``received``/``not_received``
    enum, so any LLM-set sot_link_received is dropped and the value is normalized from
    the transcript here. (Without this, "false" fell through the decide's else branch to
    the thank-and-close reply even when the borrower said the link had not arrived.)
    """
    if awaiting_slot != "sot_link_received":
        return commands
    low = (transcript or "").strip().lower()
    value = "not_received" if any(c in low for c in _SOT_LINK_NOT_RECEIVED_CUES) else "received"
    commands = [
        c
        for c in commands
        if not (c.command == "set_slot" and c.name == "sot_link_received")
    ]
    return [*commands, Command(command="set_slot", name="sot_link_received", value=value)]


# Commitment steps where a genuine "I can't pay / don't know when" reply means the
# borrower has reversed on the commitment (NOT a day/time change). At these steps we
# hand off to a human via the transfer objection instead of re-asking the time (which
# would burn the repair retries and end in a generic callback). NB: the offer/push
# intent steps (sot_payment_intent / sot_payment_intent_2) are excluded — their own
# willing/unwilling routing already sends "not willing" into the push.
SOT_REVERSAL_SLOTS: frozenset[str] = frozenset(
    {
        "sot_customer_time",
        "sot_commit_timing",
        "sot_ondue_decision",
        "sot_afterdue_decision",
        "sot_final_confirm",
    }
)
# Strong inability / no-timeline cues. Deliberately multi-word so a mere day change
# ("aaj nahi kal") does NOT match — only a real refusal ("payment nahi kar paunga",
# "pata nahi kab") does.
_SOT_REFUSAL_CUES: tuple[str, ...] = (
    "nahi kar paunga", "nahi kar paungi", "nahi kar sakta", "nahi kar sakti",
    "nahi de paunga", "nahi de paungi", "nahi de sakta",
    "nahi ho payegi", "nahi ho payega", "nahi ho paega", "nahi ho paegi",
    "payment nahi kar", "pay nahi kar", "pay nahi ho", "pay nahi paunga",
    "pata nahi kab", "keh nahi sakta", "abhi nahi keh", "bata nahi sakta",
    "cant pay", "can't pay", "cannot pay", "won't be able", "wont be able",
    "not able to pay", "unable to pay",
    "नहीं कर पाऊंगा", "नहीं कर पाऊँगा", "नहीं कर सकता", "नहीं कर सकती",
    "नहीं दे पाऊंगा", "नहीं हो पाएगी", "नहीं हो पायेगा", "पता नहीं कब",
    "पेमेंट नहीं कर",
)


def _sot_dispute_flow(transcript: str) -> str | None:
    """Return the transfer objection flow for a hard dispute in ``transcript``, else None.

    Hard disputes ("I never took this loan", "your charges are wrong", a death in the
    family, a frozen bank account) are legitimate exits that must state the right script
    and hand to a human — pushing them through the collection ladder is wrong. On-rails
    the objection KB is suppressed (see SOT_ONRAILS_FLOWS), so we match cues here
    deterministically instead of relying on retrieval/LLM. Matching is intentionally
    tolerant of ASR word-drops: e.g. "never took the loan" only needs a loan token plus a
    denial token, since the ASR frequently drops the "nahi"/word order.
    """
    low = (transcript or "").lower()
    # Never took the loan / not my loan / no such loan exists. Covers both denial of
    # *taking* the loan ("nahi liya") and denial of its *existence* ("koi loan nahi hai",
    # "loan hai hi nahi") — the latter was slipping through and looping on the last call.
    has_loan = "loan" in low or "लोन" in low
    loan_denials = (
        "nahi liya", "nahin liya", "nhi liya", "liya hi nahi", "liya nahi",
        "kabhi nahi liya", "never took", "not taken", "didnt take", "didn't take",
        "apply hi nahi", "mera nahi", "mera loan nahi",
        # Existence-denial phrasings.
        "koi loan nahi", "koi loan nahin", "loan nahi hai", "loan nahin hai",
        "loan hai hi nahi", "loan hi nahi", "no loan", "no such loan", "dont have",
        "don't have", "not mine",
        "नहीं लिया", "लिया ही नहीं", "लिया नहीं", "अप्लाई ही नहीं", "मेरा नहीं",
        "कोई लोन नहीं", "लोन नहीं है", "लोन ही नहीं", "लोन है ही नहीं",
    )
    if has_loan and any(d in low for d in loan_denials):
        return "sot_obj_never_loan"
    # Disputed repayment amount / wrong or extra charges.
    charge_cues = (
        "galat charge", "wrong charge", "extra charge", "faltu charge",
        "charge hata", "charges hata", "charge kam kar", "charges kam kar",
        "galat amount", "wrong amount", "amount galat", "itna nahi liya",
        "zyada charge", "unnecessary charge",
        "गलत चार्ज", "गलत अमाउंट", "एक्स्ट्रा चार्ज", "चार्ज हटा", "अमाउंट गलत",
    )
    if any(c in low for c in charge_cues):
        return "sot_obj_wrong_amount"
    # Bereavement in the family.
    death_cues = (
        "death ho", "death in family", "guzar ga", "guzar gay", "nahi rahe",
        "mrityu", "dehant",
        "मृत्यु", "गुज़र ग", "गुजर ग", "देहांत", "नहीं रहे",
    )
    if any(c in low for c in death_cues):
        return "sot_obj_death"
    # Frozen / blocked bank account.
    frozen_cues = (
        "account freeze", "account frozen", "account block", "account band",
        "account seize", "khata freeze",
        "खाता फ्रीज", "अकाउंट ब्लॉक", "अकाउंट फ्रीज",
    )
    if any(c in low for c in frozen_cues):
        return "sot_obj_frozen_account"
    return None


def _coerce_sot_dispute(
    commands: list[Command], transcript: str, *, on_rails: bool
) -> tuple[list[Command], bool]:
    """Start the matching transfer objection for a hard dispute raised while on-rails.

    Returns (commands, fired). Only fires on-rails (inside the offer/push/commit ladder),
    where objection retrieval is otherwise suppressed; off-rails the normal
    retrieval + LLM path already routes disputes. This ensures a borrower who denies the
    loan or disputes the charges mid-push gets the correct script + human transfer instead
    of being pushed again.
    """
    if not on_rails:
        return commands, False
    flow = _sot_dispute_flow(transcript)
    if flow is None:
        return commands, False
    return [Command(command="start_flow", flow=flow)], True


# Push/offer intent steps. A borrower answer here is a yes/no to "will you pay today".
# The LLM (esp. non-strict Groq JSON) skews to "refused" even on clear agreement
# ("haan aaj kar dunga") and hedged agreement ("theek hai koshish karunga"), so the
# ladder keeps pushing a borrower who already said yes and only exits by exhaustion.
SOT_PUSH_INTENT_SLOTS: frozenset[str] = frozenset(
    {
        "sot_payment_intent",
        "sot_payment_intent_2",
        "sot_payment_intent_3",
        "sot_payment_intent_4",
        "sot_payment_intent_5",
    }
)
# Affirmative / commit-to-pay cues (agreement, incl. soft "I'll try").
_SOT_WILLING_CUES: tuple[str, ...] = (
    "haan", "haa", "haanji", "haan ji", "ji haan", "ho jayega", "ho jayegi",
    "theek hai", "thik hai", "theek", "thik", "ok", "okay", "okey",
    "kar dunga", "kar dungi", "kar dena", "kar denge", "kar deta",
    "karunga", "karungi", "karenge", "kar lunga", "kar lungi", "kar leta",
    "koshish", "de dunga", "de dungi", "de deta",
    "bilkul", "zaroor", "jaroor", "abhi kar", "abhi hi",
    "हाँ", "हां", "ठीक", "कर दूंगा", "कर दूँगा", "करूंगा", "करूँगा", "कर लूंगा",
    "कोशिश", "हो जाएगा", "हो जाएगी", "बिल्कुल", "ज़रूर", "दे दूंगा",
)
# Markers that flip an affirmative to "not today" (a future day), an outright no, or an
# ALREADY-PAID claim (past tense) — in those cases the answer is NOT "willing today", so
# leave the LLM's value alone (already_paid has its own terminal branch).
_SOT_WILLING_DISQUALIFIERS: tuple[str, ...] = (
    "kal", "parso", "parson", "parason", "agle", "next week", "next month",
    "baad me", "baad mein", "nahi", "nahin", "nhi", " mat ", "na karu",
    "kar diya", "de diya", "diya hai", "ho gaya", "ho chuka", "kar chuka",
    "already", "paid", "pay kar diya", "payment ho",
    "कल", "परसों", "परसो", "अगले", "बाद में", "बाद मे", "नहीं", "नही", "मत",
    "कर दिया", "दे दिया", "हो गया", "हो चुका", "कर चुका", "दिया है",
)


def _coerce_sot_push_willing(
    commands: list[Command], awaiting_slot: str, transcript: str
) -> tuple[list[Command], bool]:
    """Force ``willing`` when the borrower agrees to pay at a push/offer intent step.

    Returns (commands, fired). Fires only while awaiting a push-intent slot and only
    when the transcript has a clear affirmative and no future-day / negation marker
    (so "haan kal karunga" or "aaj nahi" are left as-is). This exits the push ladder
    into the commit script the moment the borrower says yes, instead of pushing again.
    """
    if awaiting_slot not in SOT_PUSH_INTENT_SLOTS:
        return commands, False
    # Respect an explicit already_paid / willing classification from the LLM — only a
    # (wrong) "refused" or a missing value should be overridden.
    existing = next(
        (c for c in commands if c.command == "set_slot" and c.name == awaiting_slot),
        None,
    )
    if existing is not None and str(existing.value or "").lower() in {"willing", "already_paid"}:
        return commands, False
    low = (transcript or "").lower()
    if any(bad in low for bad in _SOT_WILLING_DISQUALIFIERS):
        return commands, False
    if not any(cue in low for cue in _SOT_WILLING_CUES):
        return commands, False
    # Drop a mis-set value for this slot and any bare clarify, then assert willing.
    kept = [
        c
        for c in commands
        if not (c.command == "set_slot" and c.name == awaiting_slot)
        and c.command != "clarify"
    ]
    kept.append(Command(command="set_slot", name=awaiting_slot, value="willing"))
    return kept, True


def _coerce_sot_commit_reversal(
    commands: list[Command], awaiting_slot: str, transcript: str
) -> tuple[list[Command], bool]:
    """Route a genuine can't-pay/no-timeline refusal at a commitment step to transfer.

    Returns (commands, fired). When the borrower is asked WHEN they will pay (or to
    confirm a commitment) and instead says they can't pay / don't know when, we start
    the no-timeline transfer objection (hand to a human) rather than suppressing it and
    re-asking the time. A reply that actually supplies a time/day is left untouched so
    the normal timing/confirm logic handles it.
    """
    if awaiting_slot not in SOT_REVERSAL_SLOTS:
        return commands, False
    low = (transcript or "").lower()
    if not any(cue in low for cue in _SOT_REFUSAL_CUES):
        return commands, False
    supplied_time = any(
        c.command == "set_slot"
        and c.name in {"sot_customer_time", "sot_commit_timing"}
        and str(c.value or "").strip()
        and str(c.value).strip().lower() not in {"unwilling", "no", "none", "unknown"}
        for c in commands
    )
    if supplied_time:
        return commands, False
    return [Command(command="start_flow", flow="sot_obj_no_timeline")], True


def _clarify_if_ambiguous(
    commands: list[Command],
    candidate_flows: list[dict[str, Any]],
    *,
    delta: float,
) -> tuple[list[Command], bool]:
    """F6: ask to clarify instead of guessing when the top-2 flow candidates ~tie.

    Only fires when the LLM's sole actionable command is a single start_flow (no
    set_slot alongside it) and the two highest-scoring candidates are different
    flows scoring within ``delta`` of each other. Returns (commands, fired).
    """
    starts = [c for c in commands if c.command == "start_flow"]
    if len(starts) != 1 or any(c.command == "set_slot" for c in commands):
        return commands, False
    scored = sorted(
        (
            (str(c.get("name", "")), float(c.get("score") or 0.0))
            for c in candidate_flows
            if c.get("name")
        ),
        key=lambda pair: pair[1],
        reverse=True,
    )
    if len(scored) < 2:
        return commands, False
    (top_name, top_score), (_, second_score) = scored[0], scored[1]
    if scored[0][0] == scored[1][0] or (top_score - second_score) > delta:
        return commands, False
    return [Command(command="clarify")], True


def _merge_pinned_flow_candidates(
    candidate_flows: list[dict[str, Any]],
    pinned_names: list[str],
    flows: FlowSet,
) -> list[dict[str, Any]]:
    """Layer 0: guarantee critical flows are always start_flow candidates.

    Dense KB retrieval has known recall/negation weaknesses (NevIR), so a borrower
    asking "kaise pay karun" can fail to surface ``sot_obj_link_request`` while an
    opposite-intent flow ranks higher. We append the pinned flows — with their local
    description and no KB score — so the LLM can always route to them. Pinned entries
    carry ``score=None`` so the confidence floor treats them as exempt (they were not
    retrieval-ranked). Already-present candidates are left untouched.
    """
    if not pinned_names:
        return candidate_flows
    present = {str(c.get("name", "")) for c in candidate_flows}
    merged = list(candidate_flows)
    for name in pinned_names:
        if name in present:
            continue
        flow = flows.flows.get(name)
        if flow is None:
            continue
        merged.append({"name": name, "description": flow.description, "score": None})
    return merged


def _suppress_low_confidence_flow_jumps(
    commands: list[Command],
    candidate_flows: list[dict[str, Any]],
    *,
    pinned_names: frozenset[str],
    floor: float,
) -> tuple[list[Command], bool]:
    """Layer 3: drop a start_flow backed only by a weak KB retrieval score.

    Applied while the borrower is answering a scripted collect question. A jump whose
    chosen flow scored below ``floor`` is a likely false digression (dense retrieval
    ranks near-miss / opposite-intent flows highly), so we suppress it and let a
    co-emitted set_slot (the borrower's actual answer) or a re-ask clarify handle the
    turn. Flows with no numeric KB score (pinned or deterministically coerced) are
    exempt, as are names in ``pinned_names``.
    """
    if floor <= 0:
        return commands, False
    scores: dict[str, Any] = {
        str(c.get("name", "")): c.get("score") for c in candidate_flows
    }
    kept: list[Command] = []
    suppressed = False
    for cmd in commands:
        if cmd.command == "start_flow":
            name = str(cmd.flow or "")
            score = scores.get(name)
            if name not in pinned_names and score is not None and float(score) < floor:
                suppressed = True
                continue
        kept.append(cmd)
    if suppressed and not any(
        c.command in {"start_flow", "set_slot", "cancel_flow"} for c in kept
    ):
        kept.append(Command(command="clarify"))
    return kept, suppressed


DISPUTE_EVIDENCE_KEY = "_dispute_evidence"


def _dispute_evidence_this_turn(
    transcript: str,
    proposed_commands: list[Command],
    dispute_flows: frozenset[str],
) -> str | None:
    """Which high-stakes dispute theme the borrower expressed this turn, if any.

    Evidence must reflect what the borrower actually said — NOT merely that a dispute
    flow was a retrieval/pinned candidate (a pinned dispute flow is a candidate on every
    turn, so candidate-presence would fire false evidence). The two valid signals are:
    the deterministic dispute matcher recognizes the utterance, or the LLM's *proposed*
    commands (pre-suppression) include a start_flow into a dispute flow — i.e. the model
    read this utterance as that dispute even if the floor later suppressed it.
    """
    det = _sot_dispute_flow(transcript)
    if det in dispute_flows:
        return det
    for cmd in proposed_commands:
        if cmd.command == "start_flow" and str(cmd.flow or "") in dispute_flows:
            return str(cmd.flow)
    return None


def _accumulate_dispute_evidence(
    state: ConversationState,
    commands: list[Command],
    evidence_theme: str | None,
    *,
    bar: int,
) -> tuple[ConversationState, list[Command], str | None]:
    """Cross-turn evidence accumulator for high-stakes disputes.

    A genuine dispute can score just under the confidence floor on every turn and be
    suppressed each time — the last-call failure where "loan hai hi nahi" was proposed
    by the LLM at ~0.56 three turns running and dropped each time, so the bot never
    honored it and looped. We tally per-theme evidence (see
    :func:`_dispute_evidence_this_turn`) across turns; once a theme reaches ``bar``
    corroborating turns we force its start_flow even though no single turn crossed the
    floor. Scoped to dispute themes only (asymmetric cost: honoring a real dispute
    matters far more than a rare over-eager route). Also serves as the intent-level
    repetition guard for disputes. Returns (state, commands, forced_flow_or_None).
    """
    updated = state.model_copy(deep=True)
    slots = dict(updated.slots)
    counts: dict[str, int] = dict(slots.get(DISPUTE_EVIDENCE_KEY) or {})

    forced: str | None = None
    if evidence_theme:
        already_routing = any(
            cmd.command == "start_flow" and str(cmd.flow or "") == evidence_theme
            for cmd in commands
        )
        if already_routing:
            # Deterministic coercion or an allowed jump is already routing it — no need
            # to accumulate; clear the counter so a later unrelated turn starts fresh.
            counts[evidence_theme] = 0
        else:
            counts[evidence_theme] = int(counts.get(evidence_theme, 0)) + 1
            if bar > 0 and counts[evidence_theme] >= bar:
                forced = evidence_theme
                counts[evidence_theme] = 0
                commands = [Command(command="start_flow", flow=evidence_theme)]

    slots[DISPUTE_EVIDENCE_KEY] = counts
    updated.slots = slots
    return updated, commands, forced


def _resolve_effective_flows(
    flows: FlowSet,
    brand_pack: BrandOverridePack | None,
) -> tuple[FlowSet, bool, str | None]:
    """Merge brand overrides onto platform responses; degrade on validation failure."""
    if brand_pack is None:
        return flows, False, None
    try:
        effective = merge_response_overrides(flows.responses, brand_pack, _REPLY_MANIFEST)
        flows_eff = FlowSet(flows=flows.flows, responses=effective)
        return flows_eff, False, None
    except OverrideValidationError as exc:
        reason = "; ".join(f"{error.reply_id}:{error.code}" for error in exc.errors)
        logger.warning("Brand override pack rejected: %s", reason)
        return flows, True, reason


def gate_clock_from_state(
    state: ConversationState,
    tenant_cfg: Any,
) -> datetime | None:
    """Use call_date at 10:00 local when set — stabilizes gate window for replay/tests."""
    raw = state.slots.get("call_date") or state.slots.get("today")
    if not isinstance(raw, str) or len(raw) < 10:
        return None
    try:
        call_day = date.fromisoformat(raw[:10])
        tz = ZoneInfo(tenant_cfg.call_window_timezone)
        return datetime(call_day.year, call_day.month, call_day.day, 10, 0, tzinfo=tz)
    except ValueError:
        return None


def sync_borrower_from_state(borrower: BorrowerRecord, state: ConversationState) -> BorrowerRecord:
    updated = borrower.model_copy(deep=True)
    loan = dict(updated.loan)
    for key in ("amount_due", "dpd", "bucket"):
        if key in state.slots:
            loan[key] = state.slots[key]
    updated.loan = loan
    if "compliance_flags" in state.slots:
        updated.compliance_flags = dict(state.slots["compliance_flags"])
    if state.slots.get("identity_ok"):
        identity = dict(updated.identity)
        identity["identity_ok"] = True
        updated.identity = identity
    updated = sync_trust_on_persist(updated, trigger="turn_persist")
    updated = sync_risk_on_persist(updated, trigger="turn_persist")
    return updated


def process_outbound_reply(
    draft_reply: str,
    state: ConversationState,
    request: TurnRequest,
    *,
    candidate_flows: list[dict[str, Any]] | None = None,
    commands: list[dict[str, Any]] | None = None,
    actions_called: list[str] | None = None,
    safety_reason: str | None = None,
    latency: TurnLatencyProfile | None = None,
    llm_calls: int = 0,
    now: datetime | None = None,
    resolved: ResolvedReply | None = None,
    manifest_version: str | None = None,
    brand_pack: BrandOverridePack | None = None,
    pack_rejected: bool = False,
    pack_rejected_reason: str | None = None,
) -> tuple[str, ConversationState, bool, TurnAuditChain]:
    """Apply compliance gate and build audit chain."""
    tenant_cfg = tenant_config(request.tenant_id)
    gate_result = gate(
        draft_reply,
        state,
        tenant_cfg,
        inbound_transcript=request.transcript,
        now=now or gate_clock_from_state(state, tenant_cfg),
    )

    updated = state
    transfer = bool(state.slots.get("transfer_to_human")) or gate_result.transfer_to_human

    audit_id = str(uuid.uuid4())
    latency_data: dict[str, float | dict[str, float]] = (
        latency.to_dict() if latency is not None else {}
    )
    stages_raw = latency_data.get("stages", {})
    stages: dict[str, float] = stages_raw if isinstance(stages_raw, dict) else {}
    chain = TurnAuditChain(
        audit_id=audit_id,
        call_id=request.call_id,
        borrower_id=request.borrower_id,
        tenant_id=request.tenant_id,
        ts=datetime.now(UTC).isoformat(),
        candidate_flows=candidate_flows or [],
        commands=commands or [],
        actions_called=actions_called or [],
        safety_preempted=safety_reason is not None,
        safety_reason=safety_reason,
        gate_verdict=gate_result.verdict,
        gate_level=gate_result.level,
        gate_reason=gate_result.reason,
        final_reply=gate_result.text,
        transfer_to_human=transfer or gate_result.transfer_to_human,
        latency_ms=stages,
        engine_internal_ms=float(cast(float, latency_data.get("engine_internal_ms", 0.0))),
        external_ms=float(cast(float, latency_data.get("external_ms", 0.0))),
        llm_calls=llm_calls,
        reply_id=resolved.reply_id if resolved else None,
        variant_index=resolved.variant_index if resolved else None,
        language=resolved.language if resolved else None,
        tone_register=resolved.tone_register if resolved else None,
        agent_id=request.agent_id,
        pack_id=brand_pack.pack_id if brand_pack is not None else request.pack_id,
        manifest_version=manifest_version,
        pack_rejected=pack_rejected,
        pack_rejected_reason=pack_rejected_reason,
    )
    return gate_result.text, updated, chain.transfer_to_human, chain


def safety_check_transcript(
    request: TurnRequest,
    state: ConversationState,
) -> tuple[ConversationState, str | None]:
    """Run safety pre-empt; return updated state and optional early reply text."""
    tenant_cfg = tenant_config(request.tenant_id)
    safety = safety_preempt(
        request.transcript,
        state,
        tenant_cfg,
        emotion_label=state.slots.get("emotion"),
        emotion_intensity=state.slots.get("emotion_intensity"),
    )
    if safety is None:
        return state, None
    updated = apply_safety_to_state(state, safety)
    return updated, safety.reply_text


async def _persist_turn(
    memory: Any,
    state: ConversationState,
    borrower: BorrowerRecord,
    request: TurnRequest,
    audit_chain: TurnAuditChain,
) -> str:
    borrower = sync_borrower_from_state(borrower, state)
    borrower = sync_hardships_on_persist(borrower, state)
    borrower = sync_compliance_notes_on_persist(borrower, state)
    borrower = sync_followup_on_persist(borrower, state)
    borrower = sync_refusal_negotiation_on_persist(borrower, state)
    borrower = sync_dispute_on_persist(borrower, state)
    borrower = sync_risk_on_persist(borrower, trigger="turn_persist")
    borrower = sync_emotion_on_persist(borrower, state=state, trigger="turn_persist")
    borrower = sync_persona_on_persist(borrower, state=state, trigger="turn_persist")
    borrower = sync_recovery_on_persist(borrower, state=state, trigger="turn_persist")
    audit_chain.recovery = dict(borrower.recovery)
    cleaned = state.model_copy(deep=True)
    slots = dict(cleaned.slots)
    slots.pop("opt_out_ack_this_turn", None)
    slots.pop("compliance_note_pending", None)
    for key in (
        "ptp_record_pending",
        "broken_ptp_record_pending",
        "payment_link_record_pending",
        "callback_pending",
        "call_context_note_pending",
        "refusal_record_pending",
        "negotiation_packet_pending",
        "grievance_record_pending",
        "dispute_record_pending",
    ):
        slots.pop(key, None)
    cleaned.slots = slots
    await memory.save_state(cleaned)
    await memory.save_borrower(borrower)
    audit_record = build_turn_audit_record(audit_chain)
    await memory.append_audit(
        audit_record.event,
        call_id=request.call_id,
        borrower_id=request.borrower_id,
        tenant_id=request.tenant_id,
    )
    return audit_record.audit_id


async def _stash_brand_pack(
    state: ConversationState,
    override_provider: OverrideProvider,
    request: TurnRequest,
) -> BrandOverridePack | None:
    pack = await override_provider.get_pack(
        agent_id=request.agent_id,
        pack_id=request.pack_id,
    )
    if pack is not None:
        state.slots["brand_override_pack_id"] = pack.pack_id
        state.slots["brand_override_agent_id"] = pack.agent_id
    return pack


async def _run_safety_early_exit(
    request: TurnRequest,
    state: ConversationState,
    borrower: BorrowerRecord,
    memory: Any,
    latency: TurnLatencyProfile,
    turn_span: Any,
    llm_calls: int,
    safety_reply: str,
    *,
    brand_pack: BrandOverridePack | None = None,
) -> TurnResponse:
    state = apply(
        state,
        [
            Event(
                ts=datetime.now(UTC).isoformat(),
                kind="safety_preempt",
                data={"transcript_len": len(request.transcript)},
            ),
        ],
    )
    state.attempts += 1

    reply_text, state, transfer, audit_chain = process_outbound_reply(
        safety_reply,
        state,
        request,
        safety_reason="safety_preempt",
        latency=latency,
        llm_calls=llm_calls,
        manifest_version=MANIFEST_VERSION,
        brand_pack=brand_pack,
    )

    log_turn_decision(
        session_id=request.call_id,
        transcript=request.transcript,
        borrower=borrower,
        kb_candidates=[],
        commands=[],
        rejected_slots=[],
        state=state,
        reply_id=None,
        gate_verdict=audit_chain.gate_verdict,
        gate_reason=audit_chain.gate_reason,
        draft_reply=safety_reply,
        final_reply=reply_text,
    )

    with StageTimer(latency, "persist"):
        audit_id = await _persist_turn(memory, state, borrower, request, audit_chain)

    annotate_turn_span(turn_span, chain=audit_chain, latency=latency, llm_calls=llm_calls)
    return TurnResponse(
        reply_text=reply_text,
        end_call=False,
        transfer_to_human=transfer,
        actions_executed=[],
        disposition=None,
        state_version=state.version,
        audit_id=audit_id,
    )


async def _run_closed_early_exit(
    request: TurnRequest,
    state: ConversationState,
    borrower: BorrowerRecord,
    memory: Any,
    latency: TurnLatencyProfile,
    turn_span: Any,
    llm_calls: int,
    *,
    brand_pack: BrandOverridePack | None = None,
) -> TurnResponse:
    """Terminal turn: the call was already closed (hangup/transfer) on a prior turn.

    Once a flow has hung up or handed off, a late barge-in ("ok, bye") must NOT restart
    the script — otherwise the call sits on a generic clarify with an empty flow stack
    and never disconnects. We skip command-gen/executor entirely and just re-issue
    end_call so the carrier tears the leg down. No line is spoken (the closing/handoff
    line already played on the turn that set the close).

    Exception: while a WARM TRANSFER is pending (agent still ringing), end_call is
    suppressed — ending the bot leg would tear the whole Stasis-owned call down before
    the agent joins. Teardown is orchestrator-driven in every transfer outcome
    (complete drops the AI leg; failure hangs up the customer leg), so the call can
    never idle forever.
    """
    transfer_pending = str(state.slots.get("transfer_status") or "") == "pending"
    state = apply(
        state,
        [
            Event(
                ts=datetime.now(UTC).isoformat(),
                kind="call_closed",
                data={"transcript_len": len(request.transcript)},
            ),
        ],
    )
    state.attempts += 1
    _, state, transfer, audit_chain = process_outbound_reply(
        "",
        state,
        request,
        safety_reason=None,
        latency=latency,
        llm_calls=llm_calls,
        manifest_version=MANIFEST_VERSION,
        brand_pack=brand_pack,
    )
    log_turn_decision(
        session_id=request.call_id,
        transcript=request.transcript,
        borrower=borrower,
        kb_candidates=[],
        commands=[],
        rejected_slots=[],
        state=state,
        reply_id=None,
        gate_verdict=audit_chain.gate_verdict,
        gate_reason="call_closed",
        draft_reply="",
        final_reply="",
    )
    with StageTimer(latency, "persist"):
        audit_id = await _persist_turn(memory, state, borrower, request, audit_chain)
    annotate_turn_span(turn_span, chain=audit_chain, latency=latency, llm_calls=llm_calls)
    return TurnResponse(
        reply_text="",
        end_call=not transfer_pending,
        transfer_to_human=transfer,
        actions_executed=[],
        disposition=(
            str(state.slots["disposition"])
            if state.slots.get("disposition") is not None
            else None
        ),
        state_version=state.version,
        audit_id=audit_id,
    )


async def handle_turn(
    request: TurnRequest,
    *,
    memory: Any,
    kb: Any,
    llm: Any,
    tools: Any,
    flows: FlowSet | None = None,
    overrides: OverrideProvider | None = None,
    on_gated_reply: Any | None = None,
) -> TurnResponse:
    """Full turn loop: safety → retrieval → command_gen → executor → nlg → gate → persist."""
    latency = TurnLatencyProfile()
    llm_calls = 0
    if flows is None:
        flows = get_flow_set()
    override_provider = overrides or NullOverrideProvider()
    tenant_cfg = tenant_config(request.tenant_id)
    brand_pack: BrandOverridePack | None = None
    pack_rejected = False
    pack_rejected_reason: str | None = None

    with turn_trace(request.call_id, request.borrower_id, request.tenant_id) as turn_span:
        with StageTimer(latency, "load_state"):
            state = await memory.load_state(request.call_id)
            if state is None:
                state = new_conversation_state(
                    request.call_id,
                    request.tenant_id,
                    request.borrower_id,
                )
            borrower = await memory.load_borrower(request.borrower_id)
            settings = get_settings()
            sot_test_mode = settings.test_mode and request.tenant_id == settings.test_tenant_id
            if sot_test_mode:
                # TEST_MODE on the salary_on_time tenant always uses the hardcoded SOT
                # borrower so the script renders offer/discount/due-date even when the
                # dialer phone resolves to an unrelated default-tenant row in Postgres.
                from app.memory.test_borrower import hardcoded_test_borrower

                borrower = hardcoded_test_borrower(request.borrower_id or "sot_test_borrower")
            elif borrower is None:
                if settings.test_mode:
                    from app.memory.test_borrower import hardcoded_test_borrower

                    borrower = hardcoded_test_borrower(request.borrower_id)
                else:
                    borrower = BorrowerRecord(borrower_id=request.borrower_id)
            state = hydrate_from_borrower(state, borrower)
            if settings.test_mode:
                from app.memory.test_borrower import apply_test_borrower_slots

                state = apply_test_borrower_slots(state, borrower)
            state = hydrate_followup_from_borrower(state, borrower)
            state = apply_trust_to_state(state, borrower)
            state = apply_risk_to_state(state, borrower)
            state = apply_persona_to_state(state, borrower)
            state = apply_recovery_to_state(state, borrower)
            if request.turn_meta.get("call_date"):
                state.slots["call_date"] = request.turn_meta["call_date"]
            if request.turn_meta.get("force_flow"):
                state.slots["_force_test_flow"] = str(request.turn_meta["force_flow"])
            borrower_ctx = normalize_borrower_context(request.turn_meta.get("borrower_context"))
            lookup_by_phone = getattr(memory, "lookup_borrower_by_phone", None)
            phone = borrower_ctx.get("phone") if borrower_ctx else None
            if phone and callable(lookup_by_phone) and not sot_test_mode:
                if request.borrower_id in {"", "unknown"} or not borrower.identity.get("name"):
                    db_borrower = await lookup_by_phone(phone, tenant_id=request.tenant_id)
                    if db_borrower is not None:
                        borrower = apply_borrower_context_to_record(db_borrower, borrower_ctx or {})
                        state.borrower_id = db_borrower.borrower_id
            if borrower_ctx:
                state = apply_borrower_context_to_state(state, borrower_ctx)
                borrower = apply_borrower_context_to_record(borrower, borrower_ctx)

            emotion = classify_emotion_from_turn(
                request.transcript,
                turn_meta=request.turn_meta,
                channel=request.channel,
            )
            state = apply_emotion_to_state(state, emotion)
            state, frustration_escalate = track_frustration(
                state,
                emotion=emotion.emotion,
                intensity=emotion.intensity,
                threshold=(
                    settings.sot_frustration_escalate_turns
                    if request.tenant_id == "salary_on_time"
                    else 0
                ),
            )

            state = apply_identity_entry_gate(state, flows)

            forced_flow = state.slots.get("_force_test_flow")
            if isinstance(forced_flow, str) and forced_flow in FORCE_FLOW_ALIASES:
                stack_names = {frame.flow for frame in state.flow_stack}
                already_injected = state.slots.get("_forced_flow_injected") == forced_flow
                if (
                    forced_flow not in stack_names
                    and forced_flow in flows.flows
                    and not already_injected
                ):
                    state.flow_stack.append(Frame(flow=forced_flow, step_index=0))
                    state.slots["_forced_flow_injected"] = forced_flow

            brand_pack = await _stash_brand_pack(state, override_provider, request)

        # Terminal guard: if a prior turn already closed the call (hangup_call /
        # transfer_call set end_call + sot_call_closed), any further turn is a late
        # barge-in. Do not restart the script — just re-issue end_call so the call
        # disconnects instead of idling on a generic clarify with an empty flow stack.
        if state.slots.get("sot_call_closed") or state.slots.get("end_call"):
            return await _run_closed_early_exit(
                request,
                state,
                borrower,
                memory,
                latency,
                turn_span,
                llm_calls,
                brand_pack=brand_pack,
            )

        with StageTimer(latency, "safety_preempt"):
            state, safety_reply = safety_check_transcript(request, state)
        if safety_reply is not None:
            return await _run_safety_early_exit(
                request,
                state,
                borrower,
                memory,
                latency,
                turn_span,
                llm_calls,
                safety_reply,
                brand_pack=brand_pack,
            )

        state.attempts += 1

        candidate_flows: list[dict[str, Any]] = []
        commands: list[Command] = []
        command_rejections: list[str] = []
        dispute_forced: str | None = None
        exec_result = ExecResult(state=state)
        turn_event = Event(
            ts=datetime.now(UTC).isoformat(),
            kind="turn",
            data={
                "tenant_id": request.tenant_id,
                "transcript_len": len(request.transcript),
                "channel": request.channel,
            },
        )

        # Compute the on-rails status up front. Salary_on_time: while collecting a
        # scripted slot the borrower's reply is the awaited answer, so (a) we can skip
        # KB retrieval entirely to save ~300ms/turn, and (b) objection scripts are
        # suppressed. Closed calls also skip (nothing new should start).
        sot_awaiting_slot = ""
        sot_on_rails = False
        sot_closed = False
        if request.tenant_id == "salary_on_time":
            sot_awaiting_slot = _awaiting_collect_slot(state, flows)
            active_flow = state.flow_stack[-1].flow if state.flow_stack else ""
            sot_on_rails = (
                active_flow in SOT_ONRAILS_FLOWS
                or sot_awaiting_slot in SOT_COMMIT_COLLECT_SLOTS
            )
            sot_closed = bool(
                state.slots.get("sot_call_closed") or state.slots.get("end_call")
            )
        # On-rails we normally skip retrieval to stay on-script. With digression enabled
        # we DO retrieve on-rails so the borrower can jump to a sub-flow (link/FAQ/dispute)
        # mid-script; the awaited-slot hint keeps plain answers mapping to set_slot.
        sot_digression = (
            request.tenant_id == "salary_on_time"
            and bool(getattr(settings, "sot_digression_enabled", False))
        )
        skip_retrieval = request.tenant_id == "salary_on_time" and (
            sot_closed or (sot_on_rails and not sot_digression)
        )

        candidates = []
        with span("retrieval", external=True):
            with StageTimer(latency, "retrieval", external=True):
                if not skip_retrieval:
                    candidates = await retrieve_flow_candidates(
                        kb,
                        request.transcript,
                        request.tenant_id,
                    )
        candidate_flows = [
            {"name": c.name, "description": c.description, "score": c.score} for c in candidates
        ]
        # Keep the salary_on_time script on-rails: only SOT flows are valid start_flow
        # targets, so the LLM can't derail into default-tenant flows (pay_now, etc.)
        # that the KB returns as candidates.
        sot_blocked_commands: frozenset[str] = frozenset()
        if request.tenant_id == "salary_on_time":
            candidate_flows = [
                c for c in candidate_flows if str(c.get("name", "")).startswith("sot_")
            ]
            sot_blocked_commands = SOT_BLOCKED_COMMANDS
            if sot_closed:
                candidate_flows = []
            elif sot_on_rails and not sot_digression:
                # Legacy blocklist path: suppress deflection objection scripts anywhere in
                # the push/commit journey (a frustrated "maine bola na parso" is the awaited
                # answer, not a trigger). A GENUINE can't-pay/no-timeline refusal is handled
                # after command-gen by _coerce_sot_commit_reversal (transfers to a human).
                candidate_flows = [
                    c
                    for c in candidate_flows
                    if not str(c.get("name", "")).startswith("sot_obj_")
                ]
            # Digression ON: keep the retrieved sot_ candidates (incl. sot_obj_*) so the
            # LLM can start a sub-flow mid-script. The awaited-slot hint (in the prompt +
            # response schema) is what keeps a plain answer mapping to set_slot instead of
            # a false digression — no per-flow allow/block list needed. Layer 0: also pin
            # critical flows so a KB recall/negation miss can't hide them (e.g. the
            # payment-link flow for "kaise pay karun").
            elif sot_digression:
                candidate_flows = _merge_pinned_flow_candidates(
                    candidate_flows, settings.sot_pinned_flow_list, flows
                )

        with span("command_gen", external=True):
            with StageTimer(latency, "command_gen", external=True):
                parse_result = await generate(
                    request.transcript,
                    state,
                    candidate_flows,
                    llm=llm,
                    blocked_commands=sot_blocked_commands,
                )
                commands = parse_result.commands
                command_rejections = parse_result.rejections
                llm_calls = 1

        if request.tenant_id == "salary_on_time":
            commands, dispute_fired = _coerce_sot_dispute(
                commands, request.transcript, on_rails=sot_on_rails
            )
            willing_fired = False
            if not dispute_fired:
                commands, willing_fired = _coerce_sot_push_willing(
                    commands, sot_awaiting_slot, request.transcript
                )
            if not dispute_fired and not willing_fired:
                commands = _coerce_sot_identity(
                    commands, sot_awaiting_slot, request.transcript
                )
                commands, reversal_fired = _coerce_sot_commit_reversal(
                    commands, sot_awaiting_slot, request.transcript
                )
                if not reversal_fired:
                    commands = _coerce_sot_confirm(
                        commands, sot_awaiting_slot, request.transcript
                    )
                commands = _coerce_sot_link_received(
                    commands, sot_awaiting_slot, request.transcript
                )

        # Declarative slot validation (F4, tenant-agnostic): drop set_slots that would
        # overwrite hydrated facts or fill a typed slot with the wrong kind of answer,
        # so the executor cleanly re-asks (bounded by F1/F2) instead of advancing on
        # garbage.
        commands, dropped_slots = validate_commands(commands)
        if dropped_slots:
            command_rejections = [*command_rejections, *dropped_slots]

        # Clarification on ambiguous flow candidates (F6). Gated per tenant; off for
        # salary_on_time (candidates already constrained), on for open tenants.
        if tenant_cfg.clarify_on_ambiguous_flow:
            commands, ambiguous = _clarify_if_ambiguous(
                commands, candidate_flows, delta=tenant_cfg.flow_ambiguity_delta
            )
            if ambiguous:
                command_rejections = [
                    *command_rejections,
                    "clarified ambiguous flow candidates",
                ]

        # Layer 3 (salary_on_time, digression on): while the borrower is answering a
        # scripted collect question, suppress a start_flow that is backed only by a weak
        # KB score — a likely false digression into a near/opposite-intent objection.
        # Pinned + deterministically-coerced flows are exempt (no KB score).
        if sot_digression and sot_awaiting_slot:
            commands, weak_jump_suppressed = _suppress_low_confidence_flow_jumps(
                commands,
                candidate_flows,
                pinned_names=frozenset(settings.sot_pinned_flow_list),
                floor=float(settings.sot_flow_confidence_floor),
            )
            if weak_jump_suppressed:
                command_rejections = [
                    *command_rejections,
                    "suppressed low-confidence flow jump",
                ]

        # Cross-turn evidence accumulator (salary_on_time): honor a high-stakes dispute
        # that scores just under the floor on every turn (so it is suppressed each time)
        # once it corroborates across ``bar`` turns. Evidence is what the borrower
        # expressed — the deterministic matcher or the LLM's pre-suppression proposal
        # (parse_result.commands) — never mere candidate presence. Runs after Layer 3 so
        # it can re-add a jump the floor just dropped. Scoped to dispute themes only.
        if request.tenant_id == "salary_on_time":
            evidence_theme = _dispute_evidence_this_turn(
                request.transcript,
                parse_result.commands,
                frozenset(settings.sot_dispute_flow_list),
            )
            state, commands, dispute_forced = _accumulate_dispute_evidence(
                state,
                commands,
                evidence_theme,
                bar=int(settings.sot_dispute_evidence_bar),
            )
            if dispute_forced:
                command_rejections = [
                    *command_rejections,
                    f"forced dispute route via accumulator: {dispute_forced}",
                ]

        # Label Transition Layer (LTL). Runs after all command shaping and before
        # tracker.apply. Behind LABEL_TRANSITION_ENABLED (default off). In shadow mode it
        # only observes/records labels; in enforce mode (supported providers only, e.g.
        # salary_on_time) it may rewrite command primitives. Never mutates flow_stack.
        label_decision = None
        try:
            state, commands, label_decision = run_label_transition(
                state=state,
                commands=commands,
                transcript=request.transcript,
                awaiting_slot=sot_awaiting_slot,
                candidate_flows=candidate_flows,
                tenant_id=request.tenant_id,
                flows=flows,
                settings=settings,
                dispute_forced=dispute_forced,
            )
            if label_decision is not None and label_decision.enforcement_applied:
                command_rejections = [
                    *command_rejections,
                    f"label transition enforced: {label_decision.decision}",
                ]
        except Exception:  # noqa: BLE001 — LTL must never break a live turn
            logger.exception("label_transition failed; continuing without it")
            label_decision = None

        commands_payload = [cmd.model_dump(mode="json") for cmd in commands]

        with StageTimer(latency, "tracker_apply"):
            state = apply(state, [turn_event, *commands])

        with StageTimer(latency, "priority_reorder"):
            state = apply_identity_entry_gate(state, flows)
            state = defer_collection_flows(state, flows)
            state = reorder(state, flows)

        with StageTimer(latency, "decision_overlay"):
            state = apply_decision_overlay(state, flows)

        action_runner = make_async_action_runner(tools)
        with span("executor"):
            with StageTimer(latency, "executor"):
                exec_result = await run_executor_async(state, flows, action_runner)
                state = exec_result.state

        # Warm transfer (orchestrator-only; the legacy voip.ivrobd.com POST is
        # REMOVED — it was dead, 404 in live testing). A transfer_call step set
        # transfer_requested; launch the detached driver exactly once: dial the
        # agent -> three-way on answer -> drop the AI leg (transfer/complete).
        # Requires ORCHESTRATOR_BASE_URL and a Stasis-owned call (the session
        # id resolves in the orchestrator's inbound registry). Not configured =
        # stub: log intent only; the action already set end_call in that case,
        # so the call ends cleanly, exactly like the old stub mode.
        if state.slots.get("transfer_requested") and not state.slots.get(
            "transfer_initiated"
        ):
            target = str(
                state.slots.get("transfer_target")
                or tenant_cfg.transfer_agent_number
                or settings.transfer_agent_number
            )
            reason = str(state.slots.get("transfer_reason") or "handoff")
            orchestrator_url = (os.getenv("ORCHESTRATOR_BASE_URL") or "").strip()
            if orchestrator_url and target:
                task = asyncio.create_task(
                    _drive_warm_transfer(
                        int(getattr(settings, "transfer_hold_ms", 0) or 0) / 1000.0,
                        session_uuid=state.call_id,
                        target=target,
                        caller_id=str(state.slots.get("caller_id") or ""),
                        reason=reason,
                        answer_budget_s=float(
                            getattr(settings, "transfer_answer_budget_s", 30.0) or 30.0
                        ),
                        complete_delay_s=int(
                            getattr(settings, "transfer_complete_delay_ms", 0) or 0
                        )
                        / 1000.0,
                    )
                )
                _TRANSFER_TASKS.add(task)
                task.add_done_callback(_TRANSFER_TASKS.discard)
                state.slots["transfer_initiated"] = True
                state.slots["transfer_status"] = "pending"
                state.slots["disposition"] = "TRANSFER_PENDING"
            else:
                logger.info(
                    "transfer STUB call_id=%s target=%s reason=%s "
                    "(orchestrator not configured or no agent number)",
                    state.call_id,
                    target,
                    reason,
                )
                state.slots["transfer_initiated"] = True
                state.slots["transfer_status"] = "stub"
                state.slots["disposition"] = "TRANSFER_PENDING"

        # Live WhatsApp send. A send_whatsapp_message step set whatsapp_requested +
        # captured phone/name; fire the templated message exactly once here. Detached so
        # the closing line's TTS isn't delayed by the HTTP call. Stub mode already
        # "sent" (logged) in the action, so this only does work when live.
        if (
            state.slots.get("whatsapp_requested")
            and not state.slots.get("whatsapp_sent")
            and (getattr(settings, "whatsapp_mode", "stub") or "stub").lower() == "live"
            and getattr(settings, "whatsapp_endpoint_url", "")
        ):
            wa_phone = str(
                state.slots.get("whatsapp_phone")
                or state.slots.get("phone")
                or state.slots.get("borrower_phone")
                or ""
            )
            wa_name = str(
                state.slots.get("whatsapp_name")
                or state.slots.get("customer_name")
                or state.slots.get("borrower_name")
                or ""
            )
            wa_task = asyncio.create_task(
                _send_whatsapp_bg(phone=wa_phone, name=wa_name)
            )
            _WHATSAPP_TASKS.add(wa_task)
            wa_task.add_done_callback(_WHATSAPP_TASKS.discard)
            state.slots["whatsapp_sent"] = True

        # Conversation repair (F1): count consecutive re-asks of the same slot and,
        # once the retry cap is hit, hand off gracefully instead of looping.
        had_inbound = bool((request.transcript or "").strip())
        state, repair_escalate = track_slot_reask(
            state,
            question_slot=exec_result.question_slot,
            had_inbound=had_inbound,
            max_retries=tenant_cfg.max_slot_retries,
        )

        with StageTimer(latency, "nlg"):
            flows_eff, pack_rejected, pack_rejected_reason = _resolve_effective_flows(
                flows,
                brand_pack,
            )
            if repair_escalate or frustration_escalate:
                resolved = ResolvedReply(
                    text=tenant_cfg.escalation_reply,
                    reply_id="repair_escalation",
                )
                state = mark_repair_escalation(
                    state, question_slot=exec_result.question_slot
                )
                if frustration_escalate and not repair_escalate:
                    state.slots["disposition"] = FRUSTRATION_ESCALATION_DISPOSITION
            else:
                resolved = draft_reply_resolved(
                    reply_id=exec_result.reply_id,
                    question_slot=exec_result.question_slot,
                    commands=commands,
                    state=state,
                    flows=flows_eff,
                    tenant_cfg=tenant_cfg,
                    locale=request.locale,
                    channel=request.channel,
                    transfer_to_human=exec_result.transfer_to_human,
                )
            draft = resolved.text
            state = record_outbound_context(
                state,
                reply_id=exec_result.reply_id,
                question_slot=exec_result.question_slot,
                draft=draft,
            )

        with span("gate"):
            with StageTimer(latency, "gate"):
                reply_text, state, transfer, audit_chain = process_outbound_reply(
                    draft,
                    state,
                    request,
                    candidate_flows=candidate_flows,
                    commands=commands_payload,
                    actions_called=exec_result.actions_called,
                    latency=latency,
                    llm_calls=llm_calls,
                    resolved=resolved,
                    manifest_version=MANIFEST_VERSION,
                    brand_pack=brand_pack,
                    pack_rejected=pack_rejected,
                    pack_rejected_reason=pack_rejected_reason,
                )

        log_turn_decision(
            session_id=request.call_id,
            transcript=request.transcript,
            borrower=borrower,
            kb_candidates=candidate_flows,
            commands=commands,
            rejected_slots=command_rejections,
            state=state,
            reply_id=exec_result.reply_id or resolved.reply_id,
            gate_verdict=audit_chain.gate_verdict,
            gate_reason=audit_chain.gate_reason,
            draft_reply=draft,
            final_reply=reply_text,
            raw_llm=parse_result.raw,
            question_slot=exec_result.question_slot,
            guards={
                "dispute_evidence": state.slots.get(DISPUTE_EVIDENCE_KEY) or {},
                "dispute_forced": dispute_forced,
                "frustration_turns": state.slots.get(FRUSTRATION_COUNT_KEY) or 0,
                "frustration_escalate": frustration_escalate,
                "repair_escalate": repair_escalate,
                "label_transition": (
                    label_decision.model_dump(mode="json") if label_decision else None
                ),
            },
        )

        logger.info(
            "turn_latency %s",
            json.dumps(
                {"session_id": request.call_id, "llm_calls": llm_calls, **latency.to_dict()},
                default=str,
            ),
        )

        if on_gated_reply is not None:
            await on_gated_reply(reply_text)

        # Flow-exhaustion guard: on salary_on_time the whole call is script-driven, so an
        # empty flow stack at the end of a turn means nothing is left to follow (e.g. the
        # borrower cancelled/said bye and the LLM emitted cancel_flow). Rather than idle on
        # a generic clarify forever, mark the call closed and disconnect after this reply.
        # Persisting sot_call_closed also makes any late barge-in hit the terminal guard.
        force_end_no_flow = (
            request.tenant_id == "salary_on_time"
            and not state.flow_stack
            and not (exec_result.end_call or repair_escalate)
        )
        if force_end_no_flow:
            state.slots["sot_call_closed"] = True
            state.slots["end_call"] = True
            if not state.slots.get("disposition"):
                state.slots["disposition"] = "CALL_ENDED_NO_FLOW"

        with StageTimer(latency, "persist"):
            audit_id = await _persist_turn(memory, state, borrower, request, audit_chain)

        annotate_turn_span(
            turn_span,
            chain=audit_chain,
            latency=latency,
            llm_calls=llm_calls,
        )

        disposition = exec_result.disposition
        if disposition is None and state.slots.get("disposition") is not None:
            disposition = str(state.slots["disposition"])
        if repair_escalate:
            disposition = "ESCALATED_UNCLEAR"

        return TurnResponse(
            reply_text=reply_text,
            end_call=exec_result.end_call or repair_escalate or force_end_no_flow,
            transfer_to_human=transfer or exec_result.transfer_to_human,
            actions_executed=list(exec_result.actions_called),
            disposition=disposition,
            state_version=state.version,
            audit_id=audit_id,
            reply_id=resolved.reply_id,
            variant_index=resolved.variant_index,
            language=resolved.language,
            tone_register=resolved.tone_register,
        )
