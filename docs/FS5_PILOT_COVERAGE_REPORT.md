# FS-5 Pilot Coverage Report

Generated as part of **FS-5 — Robustness + consolidated QA** (last MUST-HAVE sprint).

## Summary

| Metric | Count |
|--------|------:|
| Total pilot scenarios (tracker) | 91 |
| Routed to a named flow | **52** |
| Safety / gate only (no flow) | **8** |
| Clarify / fallback path | **24** |
| Explicit human handoff path | **7** |
| **Effective pre-pilot coverage** | **~57% flow-routed, ~100% safe path** |

Every unrecognized utterance routes to **clarify** (not silence, crash, or wrong guess). **human_handoff** is reachable via command or `human_handoff_request` flow from any state.

## Flow inventory (29 flows)

| Flow | Scenario IDs | Sprint |
|------|--------------|--------|
| `identity_verification` | ID-01/02/03 | FS-1 |
| `identity_refused` | ID-06 | FS-1 |
| `who_are_you` | ID-04 | FS-1 |
| `bot_disclosure` | ID-05 | FS-1 |
| `recording_disclosure` | CL-05 | FS-1 |
| `pay_now` | PA-01 | Phase 1 |
| `promise_to_pay` | HP-01/02 | Phase 1 |
| `dispute` | DS-01–04 | Phase 1 |
| `vulnerability` | HP-04/05/06 | Phase 1 |
| `partial_payment` | PA-04 | FS-2 |
| `already_initiated` | PA-02 | FS-2 |
| `dues_breakup` | PA-05 | FS-2 |
| `alt_channel` | PA-03 | FS-2 |
| `hardship` | HP-07–10 | FS-3 |
| `vague_ptp` | HP-03 | FS-3 |
| `opt_out` | CL-01 | FS-4 |
| `third_party` | RC-07–11 | FS-4 |
| `fraud_claim` | DS-05 | FS-4 |
| `lawyer_rep` | CL-09 | FS-4 |
| `deceased_borrower` | RC-12 | FS-4 |
| `incapacitated_borrower` | RC-13 | FS-4 |
| `harassment_complaint` | CL-02/06 | FS-4 |
| `repeat_request` | SY-03/06/08 | FS-5 |
| `off_topic_redirect` | SY-08 | FS-5 |
| `out_of_scope` | SY-02 | FS-5 |
| `human_handoff_request` | SY-09 | FS-5 |
| `balance_inquiry` | SY-10 | FS-5 |
| `due_date_inquiry` | SY-10 | FS-5 |
| `loan_terms_inquiry` | SY-10 | FS-5 |

## Routing matrix (91 scenarios)

| Category | Scenarios | Route |
|----------|----------:|-------|
| Identity & entry | 8 | Flow (FS-1) |
| Payment & PTP | 12 | Flow (Phase 1 + FS-2/3) |
| Dispute & fraud | 7 | Flow / gate |
| Hardship & vulnerability | 11 | Flow / safety |
| Compliance & handoff | 14 | Flow (FS-4) |
| Recovery & reminder | 9 | Flow (Phase 1) |
| Robustness & SY | 9 | Flow / clarify (FS-5) |
| Edge / low-confidence | 21 | **Clarify** or handoff offer |

Scenarios without a dedicated flow (24) fall through retrieval → empty or low-confidence match → **clarify** via `command_gen` + `draft_reply`. This satisfies the graceful-fallback guarantee.

## QA results (FS-5 DoD)

| Check | Result |
|-------|--------|
| Full regression (`pytest tests -m "not live_*"`) | Green |
| Compliance suite (`pytest tests/compliance/`) | Green |
| FS-1 golden | Green |
| FS-2 golden | Green |
| FS-3 golden | Green |
| FS-4 golden | Green |
| FS-5 golden | Green |
| Adversarial suite | Green |
| Cross-flow multi-signal matrix | Green (opt-out/hardship, dispute/partial, distress/identity, third-party/collection) |

## Pilot-readiness notes

- All outbound copy remains `[COMPLIANCE-REVIEW]` stubbed pending legal sign-off.
- Informational lookups (`balance_inquiry`, `due_date_inquiry`, `loan_terms_inquiry`) require `identity_ok`; identity gate parks collection-class flows until verified.
- Repeat/clarify hardening tracks `last_reply_id` / `last_question_slot` across turns; critical slots (amount, date) trigger explicit confirm after repeated repeat.
- Executor `MAX_EXECUTOR_STEPS = 256` remains the backstop against infinite loops; FS-5 repeat flow completes in ≤4 actions per turn.

## Remaining post-pilot work (non MUST-HAVE)

- Live KB / Vertex / Upstash integration tests (deselected in CI).
- Remaining ~34 scenarios benefit from KB tuning and production copy — they already route safely via clarify/handoff.
- Compliance sign-off on final wording before production calls.
