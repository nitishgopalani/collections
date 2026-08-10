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
| W2-3 | Compose + 54 fragments + router contract + unrelated lane + diversion ladder | CP-W23 | [ ] |
| W2-4 | Enforce + replay corpus + 2 live calls (on-script + messy) | FINAL-W2 = **PILOT GATE** | [ ] |
| W3 | PTP engine · computed slots · call-history+re-hydrate · obligation loop · inbound DID · 429-degrade · multi-loan · persist-async | CP-W3 | [ ] |
| W4 | Dialer audit+DNC/cadence/dedup · graceful drain · CI · summary line · /version · mining · secret rotation | CP-W4 | [ ] |

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

## Checkpoint Log (append-only)
| Date | CP | Verdict | Conditions raised → closed |
|---|---|---|---|
| 10 Aug 2026 | CP-W1C + live PREDUE | PASS | G1 (DEBT-039 speak-then-close for all 4 preempts) + G2 (DEBT-040 drain-ready gate + audioCh 8→64) deployed, sim-verified, live-verified (third_party_flip SPOKEN tts_ms=372 + bot self-ended). Ingress burst 37→14 drops (egress clipping fixed); residual ingress burst = W2/W3 observation. W1-C → 100%. |
| 10 Aug 2026 | CP-W21 | PASS | Echo filter (drop echo turns to HOLD, zero counter burn, runs before preempts) + evidence scorer 0-3 (telemetry-only, logged in turn_decision guards) + backchannel packs (paisalo/salary_on_time) + DEBT-033 call-window pin in sot_pre_closure. 18 new tests PASS; no new regressions (13 sot_pre_closure MissingSlotError pre-existing on HEAD). Next: W2-2 Commitment Gate shadow. |
| 10 Aug 2026 | CP-W22 | PASS | Commitment Gate (SHADOW): pure-function `commitment_gate(candidate, evidence, cost_table, identity_ok)` -> {execute|downgrade_to_confirm|hold} at the propose/commit seam (after command_gen+coercion+validation+clarify+dispute+LTL, before tracker_apply+executor). Cost table in tenant YAML (script/re-ask=0, speak-fact/neutral-slot=1, escalate/end_call=2, money-state/PII=3; PII keyed on identity_current). Gate consumes ONLY the W2-1 deterministic evidence score (invariant #6). Verdicts logged in turn_decision guards (gate_verdict, would_downgrade, confirm_fragment_id, gate_cost_class, gate_enforce); behaviour UNCHANGED (COMMITMENT_GATE_ENFORCE default false). 23 new tests PASS; 86+109 regression PASS; no new failures (13+1 MissingSlotError pre-existing). Carry-in: 13+1 MissingSlotError fixtures registered in known-red. Next: shadow observation week, then enforce flip (W2-4). |

## Measurements Log (append at CPs)
Replay routing accuracy · gate shadow downgrade rate · escape_hatch % · confirm-per-call · unknown_info rate · oof_class distribution · turn latency p50/p95 · live-call pass tables.
