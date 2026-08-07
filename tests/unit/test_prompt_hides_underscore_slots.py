"""V1 / CP4 — internal `_`-prefixed slots must never appear in LLM user prompts."""

import json

from app.engine.command_gen import build_user_prompt
from app.engine.nlg import REPLY_COUNTS_KEY
from app.schemas.state import ConversationState


def test_build_user_prompt_excludes_reply_counts_and_underscore_slots():
    state = ConversationState(
        call_id="v1-prompt",
        tenant_id="salary_on_time",
        borrower_id="b1",
        slots={
            REPLY_COUNTS_KEY: {"sot_obj_busy": 2},
            "_repair_counts": {"sot_payment_intent": 1},
            "_dispute_evidence": {"sot_obj_never_loan": 1},
            "_force_test_flow": "sot_opener",
            "repay_amount": 2300,
            "customer_name": "Rishabh",
            "sot_payment_intent": "refused",
        },
    )
    raw = build_user_prompt(
        "kitni payment due hai?",
        [{"name": "sot_obj_busy", "description": "busy"}],
        state,
        catalog_mode=True,
        respond_enabled=True,
    )
    payload = json.loads(raw)

    assert REPLY_COUNTS_KEY not in payload["slots"]
    assert all(not str(k).startswith("_") for k in payload["slots"])
    assert all(not str(k).startswith("_") for k in payload.get("facts", {}))
    # Whole prompt JSON must not leak the reply-count key either.
    assert REPLY_COUNTS_KEY not in raw
    assert '"_repair_counts"' not in raw
    assert '"_dispute_evidence"' not in raw
    assert payload["slots"].get("sot_payment_intent") == "refused"
    assert payload["facts"].get("repay_amount") == 2300
