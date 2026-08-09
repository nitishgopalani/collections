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
| W1-C | Policy interrupts: vulnerability, DNC capture, window close-out, third-party lock | CP-W1C + live PREDUE | [ ] |
| W2-1 | Evidence scorer + echo filter | CP-W21 | [ ] |
| W2-2 | Commitment Gate (propose→gate→commit executor split) — SHADOW | CP-W22 shadow deploy | [ ] |
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

## Checkpoint Log (append-only)
| Date | CP | Verdict | Conditions raised → closed |
|---|---|---|---|
| — | — | — | — |

## Measurements Log (append at CPs)
Replay routing accuracy · gate shadow downgrade rate · escape_hatch % · confirm-per-call · unknown_info rate · oof_class distribution · turn latency p50/p95 · live-call pass tables.
