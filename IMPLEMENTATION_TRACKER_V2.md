# IMPLEMENTATION TRACKER V2 — v1.3-final Build-Out
_Lives at docs/IMPLEMENTATION_TRACKER_V2.md in the brain repo. Updated + committed at every ✋ checkpoint. Status: `[ ]` todo · `[~]` in progress · `[R]` in review · `[x]` signed off · `[!]` blocked._
_Implementer: Cursor · Reviewer: Claude · Sign-off: Nitish · Started: 09 Aug 2026 · Base: brain 810647d, go-server 4e52063, connector 0b7a252._

## Phase Map
| Phase | What | Gate | Status | Bar |
|---|---|---|---|---|
| P0 | REPO_CONTEXT.md full-codebase scan + commit chat-only docs | CP0 | [ ] | ..........   0% |
| A2 | Multi-tenancy audit (TENANCY_AUDIT.md + register) | CP-A2 | [ ] | ..........   0% |
| DT | DETENANT refactor (scope per CP-A2 ruling) | CP-DT | [ ] | ..........   0% |
| W1-A | PLO-OOF: cue packs (willing/disqualifiers/callback/reversal), re-ask ladder, grounding forensic, B-side quick wins, t6-replay | CP-W1A | [ ] | ..........   0% |
| W1-B | H2 dead-air: asr_dead apology+close, TTS-fail×2, reply_empty, loud env | CP-W1B | [ ] | ..........   0% |
| W1-C | Policy interrupts: vulnerability, DNC capture, window close-out, third-party lock | CP-W1C + live PREDUE | [x] | ########## 100% |
| W2-1 | Evidence scorer + echo filter | CP-W21 | [x] | ########## 100% |
| W2-2 | Commitment Gate (propose→gate→commit executor split) — SHADOW | CP-W22 shadow deploy | [x] | ########## 100% |
| W2-3 | Compose + 54 fragments + router contract + unrelated lane + diversion ladder | CP-W23 | [x] | ########## 100% |
| W2-4 | Enforce + replay corpus + 2 live calls (on-script + messy) | FINAL-W2 = **PILOT GATE** | [x] | ########## 100% |
| W2-5 | Compose-selection few-shots (complaint / irrelevant / facts); hatch <5% | CP-W25 | [x] | ########## 100% |
| W2-4b | LLM-diet: D1 cue-hit skip · D2 class cache · D3 state-scoped catalog | CP-W25 | [x] | ########## 100% |
| LAD | PaisaLo scenario ladder live (ondue / postdue1 / postdue3 / NPA) | CP-LAD | [x] | ########## 100% |
| W3-1 | PTP policy engine + computed slots | CP-W31 | [x] | ########## 100% |
| W3-2 | Call-history + mid-call memory | CP-W32 | [x] | ########## 100% |
| W3-3 | Post-call obligation loop | CP-W33 | [x] | ########## 100% |
| W3-4 | Edges + debt (DID, 429, multi-loan, persist-async) | CP-W34 | [x] | ########## 100% |
| OOF | L0 topics + L1 related/ack + index recovery | CP-OOF | [x] | ########## 100% |
| UI-0 | Admin API `/admin/v0` (env-gated, YAML is the DB) | CP-UI0 | [x] | ########## 100% |
| UI-1 | Brand Console page (Vite + React + Tailwind) | CP-UI1 | [x] | ########## 100% |
| UI-3 | Test Console page (demo priority, before UI-2) | CP-UI3 | [x] | ########## 100% |
| UI-2 | Fragment Studio | CP-UI2 | [ ] | ..........   0% |
| UI-4 | Obligations Dashboard | CP-UI4 | [ ] | ..........   0% |
| W4 | Dialer audit+DNC/cadence/dedup · graceful drain · CI · summary line · /version · mining · secret rotation | CP-W4 | [ ] | ..........   0% |

**OVERALL: ~82%** `########..` — W1-C + W2-1..W2-5 + W2-4b + LAD + W3-1..W3-4 + OOF-STACK signed off. W3 CLOSED. C-3 PTP defaults PENDING-CLIENT. **UI console 3.5/5.5d** (UI-0/1/3 demo-ready). Next: W4 pilot plumbing, then UI-2 + UI-4.

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

- **DEBT-038 (PREDUE-012, CLOSED W3-4):** slot-segmented TTS cache keys — static prefix/suffix around `{customer_name}` prewarmed; live Speak matches those keys.
- **DEBT-043 (LAD, CLOSED W3-4):** consent-enum. `consent_yes`/`consent_no` cue packs + `coerce_consent` map `haan bataiye` / `haan boliye` / `to` → `yes` on `plo_consent_2min`.
- **DEBT-044 (LAD, CLOSED W3-4):** opener m2e=0. go-server `durations()` falls back to speech_end/asr_final/engine_sent/session_start → egress when `caller_end` is missing.

## Checkpoint Log (append-only)
| Date | CP | Verdict | Conditions raised → closed |
|---|---|---|---|
| 10 Aug 2026 | CP-W1C + live PREDUE | PASS | G1 (DEBT-039 speak-then-close for all 4 preempts) + G2 (DEBT-040 drain-ready gate + audioCh 8→64) deployed, sim-verified, live-verified (third_party_flip SPOKEN tts_ms=372 + bot self-ended). Ingress burst 37→14 drops (egress clipping fixed); residual ingress burst = W2/W3 observation. W1-C → 100%. |
| 10 Aug 2026 | CP-W21 | PASS | Echo filter (drop echo turns to HOLD, zero counter burn, runs before preempts) + evidence scorer 0-3 (telemetry-only, logged in turn_decision guards) + backchannel packs (paisalo/salary_on_time) + DEBT-033 call-window pin in sot_pre_closure. 18 new tests PASS; no new regressions (13 sot_pre_closure MissingSlotError pre-existing on HEAD). Next: W2-2 Commitment Gate shadow. |
| 10 Aug 2026 | CP-W22 | PASS | Commitment Gate (SHADOW): pure-function `commitment_gate(candidate, evidence, cost_table, identity_ok)` -> {execute|downgrade_to_confirm|hold} at the propose/commit seam (after command_gen+coercion+validation+clarify+dispute+LTL, before tracker_apply+executor). Cost table in tenant YAML (script/re-ask=0, speak-fact/neutral-slot=1, escalate/end_call=2, money-state/PII=3; PII keyed on identity_current). Gate consumes ONLY the W2-1 deterministic evidence score (invariant #6). Verdicts logged in turn_decision guards (gate_verdict, would_downgrade, confirm_fragment_id, gate_cost_class, gate_enforce); behaviour UNCHANGED (COMMITMENT_GATE_ENFORCE default false). 23 new tests PASS; 86+109 regression PASS; no new failures (13+1 MissingSlotError pre-existing). Carry-in: 13+1 MissingSlotError fixtures registered in known-red. Next: shadow observation week, then enforce flip (W2-4). |
| 10 Aug 2026 | CP-W23 | PASS | Compose Lane + Fragment Library + Router Contract + DEBT-041. Fragment library `paisalo_fragments.yml` (58 fragments: 51 selectable + 3 confirms + 1 terminal + 3 new redirect/scope; {G:fem\|mask} gender tokens, {slot} hydration, answers[]/safe_in/scenario/product tags). `fragment_library.py` loader + `validate_compose` (ids exist, ack pair-only, scenario/product gates, unhydrated → unknown_info) + offline compliance pass (P5.0-style, PASS zero issues). `compose` command (Command schema + fragments/oof_class). Router contract on CommandParseResult (oof_class 9 + subclass + secondary_intents + confidence telemetry-only; same LLM call; None on normal turns). UNRELATED deterministic lane (`render_unrelated_redirect`: pre/post-identity scope-boundary; world-knowledge/RAG/tools/Tier-3 OFF). Renderer (gender-resolve by persona voice position-based, slot substitution, EXACT RESUME append, never TTS-buffer replay). Diversion ladder (`_redirect_count` own counter, separate from repair). Tier-3 demotion (`escape_hatch_used`) + complaint (`complaint_raised`). DEBT-041 fix: `identity_confirm` cost class (cost 2, EXEMPT from identity_current) + pii narrowed to personal-data slots; t2 identity turn executes at evidence 2. 44 new tests PASS; 200 regression PASS; no new failures. Next: W2-4 enforce flip + replay corpus + live calls. Shadow-week verdicts appended as they occur. |
| 15 Aug 2026 | PILOT GATE (CP-W24) | PASS (conditional) | CALL A `d66ce098` on-script PASS (6 turns: E1 which-EMI execute, E3 office no-phantom-willing, confirm spoken, haan-pakka execute, repair=0). CALL B `950e271c` messy PASS (11 turns: bounded, callback execute, confirm+close, no spiral). Combined: confirm-success 2/2, hatch 3/17 ~18%, redirect_count 0, oof_class 100% null, M2E p50~1.7s / p95~2.0s. Condition: W2-5 compose-selection (complaint / weather redirect / facts / hatch<5%). W2-4 → [x]. OVERALL ~50%. |
| 15 Aug 2026 | CP-W25 | PASS | W2-5 compose few-shots (complaint/irrelevant/facts) + W2-4b LLM-diet D1/D2/D3. Replay: hatch 0/8 <5%, weather redirect_count>=1, complaint_raised. D3 A/B: catalog tokens 779 -> 266-468 (40-66% cut); misroutes 0; scope_miss 0 on live-call flows (escape valve tested). 205 W2+catalog tests PASS. |
| 15 Aug 2026 | L1-FIX (F1-F6) | landed | e1d5d837 failure: false identity, invented compose ids, set_slot text-reject, willing-confirm after no, D2 cache of rejected JSON. Fixes: fragment index + fact_caller_identity few-shot; text->value alias; id_yes bot-substring purge + yes/yes+name skip + t2 echo HOLD; unwilling vs inability; value-aware refusal confirm + pending(v) ev3; D2 write-through on parse success only. Golden 13/13 + 192 W2 regression PASS. Live L1 PASS `1debe02d`. |
| 15 Aug 2026 | CP-LAD (L1-L4) | PASS | Scenario ladder signed off. L1 ONDUE simran `1debe02d` (19922f2) · L2 PD1 neha `db767332` (f8c87b4) · L3 PD3 kabir `a8642ebb` (95563d9) · L4 NPA amit `5c6c7663` (c5ba321). Combined: hatch 0, repair 0, confirm-success 6/6. DEBT-043 consent-enum + DEBT-044 m2e=0 registered. LAD → [x]. OVERALL ~58%. STOP — W3 spec from architect. |
| 15 Aug 2026 | CP-W31 | PASS | PTP policy engine + computed slots. C-3 defaults PENDING-CLIENT (max_ptp_days=30, min_partial_pct=25, counter_max=1). >30d counter once then ptp_beyond_policy + PTP_SET. Partial 50% remainder-ask / 10% full-ask. L3+L2 replays green. W3-1 → [x]. OVERALL ~64%. STOP — W3-2 next. |
| 15 Aug 2026 | CP-W32 | PASS | Call-history + mid-call memory. Sessions-store index (R1, no new table). Repeat greeting R2 (no detail dump). PTP honour contradiction last_ptp_date=+5d → PTP_REMINDED. Payment claim → fact_payment_lag + payment_claimed. W3-2 → [x]. OVERALL ~70%. STOP — W3-3 next. |
| 15 Aug 2026 | CP-W33 | PASS | Obligation loop. dispositions_YYYYMMDD.jsonl + CSV (R1). callbacks re-queue. worklist + 30-word snippet (R2). Webhook stub only. L1-L4 + PTP live exact rows. W3-3 → [x]. OVERALL ~76%. STOP — W3-4 next. |
| 15 Aug 2026 | CP-W34 | PASS | Inbound DID INBOUND_RETURN. LLM-429 degrade survives. Multi-loan highest-DPD. Persist-async Upstash. DEBT-038/043/044 closed. W3-4 → [x]. OVERALL ~82%. W3 CLOSED. STOP — OOF-STACK next. |
| 15 Aug 2026 | CP-OOF | PASS | L0 8-pack zero-LLM. L1 related+ack_text + index recovery / honest-miss. Ack first-diversion only. Mining doc. OOF → [x]. OVERALL ~82%. STOP — W4 next. |
| 15 Aug 2026 | CP-UI0 | PASS | `/admin/v0` env-gated. Profile/fragments/dry-run/tts-preview/test-turn/exports. Invalid PUT 422. Dry-run flags prohibited. Test-turn guards on willing. UI-0 → [x]. OVERALL ~82%. UI 1.0/5.5d. STOP — UI-1 fonada-console next. |
| 15 Aug 2026 | CP-UI1 + CP-UI3 | PASS | `Main/fonada-console` Vite+React+Tailwind. Brand knobs + TTS preview + save/reload. Test chat + guards strip + fixture download. UI-1/UI-3 → [x]. OVERALL ~82%. UI 3.5/5.5d. STOP — W4 then UI-2/UI-4. |

## Measurements Log (append at CPs)
Replay routing accuracy · gate shadow downgrade rate · escape_hatch % · confirm-per-call · unknown_info rate · oof_class distribution · turn latency p50/p95 · live-call pass tables.

| 15 Aug 2026 | CP-LAD | PASS | L1 `1debe02d` + L2 `db767332` + L3 `a8642ebb` + L4 `5c6c7663`. Hatch 0, repair 0, confirm-success **6/6**, M2E non-zero ~1.0-2.5s (t1 m2e=0 = DEBT-044). LAD → [x]. OVERALL ~58%. |
| 15 Aug 2026 | CP-W31 | PASS | PTP accept/counter/flag + partial remainder/full-ask. Computed slots remaining_after / days_to_due / days_since_due. Tests 20 W3-1+L3/L2 + 112 W2 compose/enforce/diet. |
| 15 Aug 2026 | CP-W32 | PASS | Repeat greeting + PTP_REMINDED honour + payment_claimed. Tests 5 W3-2 + 20 W3-1/L3/L2. |
| 15 Aug 2026 | CP-W33 | PASS | L1-L4 + PTP_SET rows in dispositions; callback_request in callbacks; dnc snippet in worklist. Tests 2 W3-3 + 49 W3/L2/L3/P1. |
| 15 Aug 2026 | CP-W34 | PASS | Inbound 2-turn + 429 degrade + multi-loan + persist-async + consent forms + TTS segments. Tests 10 W3-4 + 16 W3-1/2/3 + 10 L2/L3. Go: m2e fallback + template split. |
| 15 Aug 2026 | CP-OOF | PASS | PM L0 zero-LLM; rashi L1 ack; identity index recovery; processing-fee honest-miss; 2nd politics no ack. 6 OOF + 57 stacked green. |
| 15 Aug 2026 | CP-UI0 | PASS | PUT invalid 422 field errors; dry-run police aayegi=fail; test-turn willing returns evidence+gate_verdict+llm_call_reason. 4/4 admin tests. |
| 15 Aug 2026 | CP-UI1 + CP-UI3 | PASS | fonada-console `b788052`. Brand + Test pages. Platform handoff in README. |
