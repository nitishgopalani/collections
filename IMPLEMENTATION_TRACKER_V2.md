# IMPLEMENTATION TRACKER V2 — v1.3-final Build-Out
_Lives at docs/IMPLEMENTATION_TRACKER_V2.md in the brain repo. Updated + committed at every ✋ checkpoint. Status: `[ ]` todo · `[~]` in progress · `[R]` in review · `[x]` signed off · `[!]` blocked._
_Implementer: Cursor · Reviewer: Claude · Sign-off: Nitish · Started: 09 Aug 2026 · Base: brain 810647d, go-server 4e52063, connector 0b7a252._

## Phase Map
| Phase | What | Gate | Status |
|---|---|---|---|
| P0 | REPO_CONTEXT.md full-codebase scan + commit chat-only docs | CP0 | [ ] |
| A2 | Multi-tenancy audit (TENANCY_AUDIT.md + register) | CP-A2 | [ ] |
| DT | DETENANT refactor (scope per CP-A2 ruling) | CP-DT | [ ] |
| W1-A | PLO-OOF: cue packs (willing/disqualifiers/callback/reversal), re-ask ladder, grounding forensic, B-side quick wins, t6-replay | CP-W1A | [ ] |
| W1-B | H2 dead-air: asr_dead apology+close, TTS-fail×2, reply_empty, loud env | CP-W1B | [ ] |
| W1-C | Policy interrupts: vulnerability, DNC capture, window close-out, third-party lock | CP-W1C + live PREDUE | [x] |
| W2-1 | Evidence scorer + echo filter | CP-W21 | [x] |
| W2-2 | Commitment Gate (propose→gate→commit executor split) — SHADOW | CP-W22 shadow deploy | [x] |
| W2-3 | Compose + 54 fragments + router contract + unrelated lane + diversion ladder | CP-W23 | [x] |
| W2-4 | Enforce + replay corpus + 2 live calls (on-script + messy) | FINAL-W2 = **PILOT GATE** | [x] |
| W2-5 | Compose-selection few-shots (complaint / irrelevant / facts); hatch <5% | CP-W25 | [x] |
| W2-4b | LLM-diet: D1 cue-hit skip · D2 class cache · D3 state-scoped catalog | CP-W25 | [x] |
| W3 | PTP engine · computed slots · call-history+re-hydrate · obligation loop · inbound DID · 429-degrade · multi-loan · persist-async | CP-W3 | [ ] |
| W4 | Dialer audit+DNC/cadence/dedup · graceful drain · CI · summary line · /version · mining · secret rotation | CP-W4 | [ ] |

**OVERALL: ~50%** — W1-C + W2-1..W2-5 + W2-4b signed off. L1-FIX F1-F6 landed (e1d5d837 replay PASS; live L1 redial pending). W3 / W4 / P0 / A2 / DT / W1-A / W1-B open. Next: L1 redial PASS, then L2-L4 ladder. Do not start W3 until L1 PASS.

## Hard invariants (breaking any = checkpoint FAIL)
1. Gate-before-side-effect (no rollback code anywhere)
2. Policy precedence: echo-filter → interrupts (no evidence veto) → scorer → router
3. Source-trust: system>hydrated>KB>fragment>borrower_claim>LLM; account_fact fails closed; slot writes source-tagged
4. Confidence = telemetry-only, never gate input
5. Router = same LLM call; OOF fields omitted on normal turns
6. Unrelated = deterministic scope-boundary, never content
7. Two ladders two counters (diversion ≠ repair)
8. Every lane renders X + canonical re-ask (never TTS-buffer replay)
9. Zero new tenant string-compares — profile fields only (post-DT rule; lint/grep check at every checkpoint: `grep -rn '"salary_on_time"\|"paisalo"' app/engine/ app/ws/` count must not grow)
10. Dead air impossible: every failure path speaks or ends loudly

## Decisions blocking (owner: Nitish/client) — mirror of MASTER_SPRINT_PLAN §5
N-1 MFI scope [ ] · D-P1..P8 fragment decisions [ ] · C-1 PaisaLo written approval packet [ ] · C-2 multi-loan model [ ] · C-3 PTP grace rules [ ] · C-4 inbound DID [ ] · N-2 secret rotation [ ]

## Known-red baseline (pre-existing; fix only where scheduled)
G-B6-01 OOC golden fixture (W1-A) · G-B6-02 willing reply_id assert (W1-A) · G-B6-03 already_paid golden (triage W1-A) · 29 test-order pollution (platform debt) · turn.py SOT aliases (DT candidate) · G-A3-02 lab slin16 (register) · persist-sync opener dependency (W3)

DEBT-033 carry-in (W2-2): 13 `test_sot_pre_closure.py` fixtures fail with `MissingSlotError: customer_name, repay_amount` — the SOT test-mode fixture does not hydrate the opener-greeting template slots. The W2-1 call-window pin removed the time-flake source; this hydration bug is separate (register, not W2-2 scope). Fixtures by name:
- `test_happy_path_offer_to_close_order`
- `test_objection_penalty_answers_and_resumes_offer`
- `test_third_party_c1_family_proceeds_third_person`
- `test_third_party_c3_restricted_never_reveals_offer`
- `test_already_paid_acknowledges_and_ends`
- `test_barge_in_after_close_disconnects`
- `test_cancel_flow_empties_stack_disconnects`
- `test_catalog_digression_resumes_parent_after_info_objection`
- `test_link_request_confirms_receipt_then_hangs_up`
- `test_link_request_not_received_resends_reassures_then_hangs_up`
- `test_link_request_not_received_llm_boolean_still_resends`
- `test_catalog_allows_non_deflection_objection_mid_collect`
- `test_catalog_link_request_routes_without_digression_flag`

Same `MissingSlotError: customer_name, repay_amount` hydration bug also fails
`tests/golden/test_w1c_call_window_close.py::test_c3_mid_call_window_cross_closes_gracefully`
(confirmed pre-existing on HEAD 0f8ea23). All 14 are the same root cause
(SOT test-mode fixture does not hydrate opener-greeting template slots);
fix is a single hydration patch, tracked as a register row.

## Debt register (append-only)
- **DEBT-041 (W2-3, MUST-FIX before enforce):** identity chicken-egg. The W2-2 gate classifies `plo_identity_response` / `sot_identity_response` as `pii` (cost 3) keyed on `identity_current`. But identity_current is set BY confirming the identity slot — so the gate would hold/downgrade the very turn that establishes identity, and the call can never reach identity_current=true. Fix (lands with W2-3): new gate class `identity_confirm` (cost 2), exempt from the `identity_current` precondition; `pii` class narrowed to personal-data slots only (`customer_name`, `phone`, `address`, `dob` — NOT identity-confirmation slots). Locking test: full-call shadow replay where the t2 identity turn verdict = `execute` at evidence 2 (the gate must NOT hold the identity-confirm turn).
- **DEBT-033 (W2-1 fold-in):** 13+1 `MissingSlotError` fixtures (see known-red above). SOT test-mode hydration gap. Register; fix is a single hydration patch (W3 candidate).
- **DEBT-042 (W2-4, register):** Pre-existing golden failures on HEAD adc9e14 (W2-3 commit), NOT caused by W2-4. Confirmed by stash-compare: 22 failures on clean HEAD vs 21 with W2-4 changes (W2-4 introduced ZERO new failures; the delta is test-order pollution). Families: (a) `test_respond_tier3.py` (7) — Tier-3 escape-hatch path, likely DEBT-034 Item-2 opener-LLM-skip off-by-one consequences; (b) `test_plo_oof_*` (5: checkpoint_replay fb6a0f02, p2_reask_laddering, p3_grounding_forensic, p4_bside_wins, p5_committed_date) — OOF replay fixtures; (c) `test_paisalo_scenarios.py` NPA (3: happy_path, refuse_twice_escalates, out_of_context_question); (d) `test_attempt_escalation_e2e.py` (1: objection_attempt_one_two_then_escalate — test_generic LLM mock emits start_flow:tg_ask instead of tg_obj_repeat on t3); (e) `test_catalog_routing.py` (1) + `test_label_transition_e2e.py` (4) — test-order pollution (platform debt); (f) `test_w1c_call_window_close.py::test_c3_mid_call_window_cross_closes_gracefully` (DEBT-033 hydration). All register-only; triage in W3.

## Checkpoint Log (append-only)
| Date | CP | Verdict | Conditions raised → closed |
|---|---|---|---|
| 10 Aug 2026 | CP-W1C + live PREDUE | PASS | G1 (DEBT-039 speak-then-close for all 4 preempts) + G2 (DEBT-040 drain-ready gate + audioCh 8→64) deployed, sim-verified, live-verified (third_party_flip SPOKEN tts_ms=372 + bot self-ended). Ingress burst 37→14 drops (egress clipping fixed); residual ingress burst = W2/W3 observation. W1-C → 100%. |
| 10 Aug 2026 | CP-W21 | PASS | Echo filter (drop echo turns to HOLD, zero counter burn, runs before preempts) + evidence scorer 0-3 (telemetry-only, logged in turn_decision guards) + backchannel packs (paisalo/salary_on_time) + DEBT-033 call-window pin in sot_pre_closure. 18 new tests PASS; no new regressions (13 sot_pre_closure MissingSlotError pre-existing on HEAD). Next: W2-2 Commitment Gate shadow. |
| 10 Aug 2026 | CP-W22 | PASS | Commitment Gate (SHADOW): pure-function `commitment_gate(candidate, evidence, cost_table, identity_ok)` -> {execute|downgrade_to_confirm|hold} at the propose/commit seam (after command_gen+coercion+validation+clarify+dispute+LTL, before tracker_apply+executor). Cost table in tenant YAML (script/re-ask=0, speak-fact/neutral-slot=1, escalate/end_call=2, money-state/PII=3; PII keyed on identity_current). Gate consumes ONLY the W2-1 deterministic evidence score (invariant #6). Verdicts logged in turn_decision guards (gate_verdict, would_downgrade, confirm_fragment_id, gate_cost_class, gate_enforce); behaviour UNCHANGED (COMMITMENT_GATE_ENFORCE default false). 23 new tests PASS; 86+109 regression PASS; no new failures (13+1 MissingSlotError pre-existing). Carry-in: 13+1 MissingSlotError fixtures registered in known-red. Next: shadow observation week, then enforce flip (W2-4). |
| 10 Aug 2026 | CP-W23 | PASS | Compose Lane + Fragment Library + Router Contract + DEBT-041. Fragment library `paisalo_fragments.yml` (58 fragments: 51 selectable + 3 confirms + 1 terminal + 3 new redirect/scope; {G:fem\|mask} gender tokens, {slot} hydration, answers[]/safe_in/scenario/product tags). `fragment_library.py` loader + `validate_compose` (ids exist, ack pair-only, scenario/product gates, unhydrated → unknown_info) + offline compliance pass (P5.0-style, PASS zero issues). `compose` command (Command schema + fragments/oof_class). Router contract on CommandParseResult (oof_class 9 + subclass + secondary_intents + confidence telemetry-only; same LLM call; None on normal turns). UNRELATED deterministic lane (`render_unrelated_redirect`: pre/post-identity scope-boundary; world-knowledge/RAG/tools/Tier-3 OFF). Renderer (gender-resolve by persona voice position-based, slot substitution, EXACT RESUME append, never TTS-buffer replay). Diversion ladder (`_redirect_count` own counter, separate from repair). Tier-3 demotion (`escape_hatch_used`) + complaint (`complaint_raised`). DEBT-041 fix: `identity_confirm` cost class (cost 2, EXEMPT from identity_current) + pii narrowed to personal-data slots; t2 identity turn executes at evidence 2. 44 new tests PASS; 200 regression PASS; no new failures. Next: W2-4 enforce flip + replay corpus + live calls. Shadow-week verdicts appended as they occur. |
| 15 Aug 2026 | PILOT GATE (CP-W24) | PASS (conditional) | CALL A `d66ce098` on-script PASS (6 turns: E1 which-EMI execute, E3 office no-phantom-willing, confirm spoken, haan-pakka execute, repair=0). CALL B `950e271c` messy PASS (11 turns: bounded, callback execute, confirm+close, no spiral). Combined: confirm-success 2/2, hatch 3/17 ~18%, redirect_count 0, oof_class 100% null, M2E p50~1.7s / p95~2.0s. Condition: W2-5 compose-selection (complaint / weather redirect / facts / hatch<5%). W2-4 → [x]. OVERALL ~50%. |
| 15 Aug 2026 | CP-W25 | PASS | W2-5 compose few-shots (complaint/irrelevant/facts) + W2-4b LLM-diet D1/D2/D3. Replay: hatch 0/8 <5%, weather redirect_count>=1, complaint_raised. D3 A/B: catalog tokens 779 -> 266-468 (40-66% cut); misroutes 0; scope_miss 0 on live-call flows (escape valve tested). 205 W2+catalog tests PASS. |
| 15 Aug 2026 | L1-FIX (F1-F6) | landed | e1d5d837 failure: false identity, invented compose ids, set_slot text-reject, willing-confirm after no, D2 cache of rejected JSON. Fixes: fragment index + fact_caller_identity few-shot; text->value alias; id_yes bot-substring purge + yes/yes+name skip + t2 echo HOLD; unwilling vs inability; value-aware refusal confirm + pending(v) ev3; D2 write-through on parse success only. Golden 13/13 + 192 W2 regression PASS. Live L1 redial pending. |

## Measurements Log (append at CPs)
Replay routing accuracy · gate shadow downgrade rate · escape_hatch % · confirm-per-call · unknown_info rate · oof_class distribution · turn latency p50/p95 · live-call pass tables.
