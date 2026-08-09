# IMPLEMENTATION TRACKER V2 — Fonada Voice Collections

_Phase map + progress bars. Maintained by the implementer._
_This file is the on-disk anchor; the chat-only `Collection/IMPLEMENTATION_TRACKER_V2.md` (0-byte on disk at scan) holds the fuller phase narrative in the IDE buffer and must be merged in by the user._

> **NOTE (P0 audit, 09 Aug 2026):** The chat-only `IMPLEMENTATION_TRACKER_V2.md` at the repo root is 0-byte on disk; its content lives only in the IDE editor buffer. This on-disk copy in `docs/` is the canonical anchor going forward. The user should paste the IDE-buffer content into this file and re-save, then `git add` it.

---

## PHASE MAP

| Phase | Status | Bar | Notes |
|---|---|---|---|
| **P0 — Repo Context Audit** | [R] | 100% | `docs/REPO_CONTEXT.md` written. CP0 signed off 09 Aug 2026. |
| A2 — Tenancy Audit + DT Refactor | [P] | 0% (planned) | `docs/TENANCY_AUDIT.md` delivered: 11 new profile fields + 1 guard + 41-orphan deletion list, 1.5d effort. Execution blocked on architect sign-off. |
| DT — Decision-Tree Hardening | [P] | 0% (planned) | Folded into A2 (§4 of `TENANCY_AUDIT.md`). |
| W1-A — SOT/PLO On-Rails Hardening | [R] | **83%** | P1-P5 done (41/41 tests pass at `958135d`); H3 (PaisaLo reversal stage) skipped — DEBT-016. Ready for architect review of residual. |
| W1-B — Identity Gate Hardening | [ ] | 0% | — |
| W1-C — Compliance Gate Hardening | [ ] | 0% | — |
| W2-1 — Evidence Scorer + Echo Filter | [ ] | 0% | — |
| W2-2 — Commitment Gate | [ ] | 0% | — |
| W2-3 — Compose Lane + Fragment Library | [ ] | 0% | — |
| W2-4 — Enforce + Replay Regression | [ ] | 0% | — |
| W3 — Tenant Isolation | [ ] | 0% | Per-tenant `TenantRuntimeProfile` field migration (DEBT-001..010). |
| W4 — Multi-Tenant Scale | [ ] | 0% | Per-tenant borrower tables (DEBT-011), script triage (DEBT-013). |

---

## CHECKPOINT LOG

### CP0 — 09 Aug 2026 — P0 Repo Context Audit
- **Status:** [R] (ready for architect sign-off).
- **Deliverable:** `docs/REPO_CONTEXT.md` (module map, turn lifecycle walk, complete tenant-conditional inventory, config/env inventory, data model, known-debt appendix).
- **Tenant-conditional counts (regex `sot_|plo_|salary_on_time|paisalo`):**
  - `app/engine/turn.py`: 78 matches (boot hint ~32; ~12 real branch points, rest are local var names / data).
  - `app/engine/nlg.py`: 30 matches (matches hint; all data — slot->reply_id dict entries).
  - `app/config.py`: 27 matches (boot hint ~5; 1 real branch at line 610, rest are `Settings` field defaults).
  - `app/engine/actions.py`: 70+ matches (action-name dispatch + slot keys — data, not branches).
  - `app/engine/scripted_coercions.py`: 7 matches (2 real branches on `profile.flow_prefix`).
  - `app/engine/label_transition.py`: 14 matches (1 real branch at line 121, rest are SOT label maps).
  - `app/engine/identity_gate.py`: 2 matches (1 hardcoded `sot_opener` flow name).
  - `app/ws/handler.py`: 9 matches (3 real branches on `is_paisalo_test` / `test_force`).
  - `app/ws/routing.py`: 50+ matches (`FORCE_FLOW_ALIASES` + `ALLOWED_OPENERS` data).
  - `app/memory/test_borrower.py`: 12 matches (2 real branches on `state.tenant_id == "paisalo"`).
- **Real branch points total: 12.** All 12 are profile-able with new `TenantRuntimeProfile` fields (mostly bools or lists). See `docs/REPO_CONTEXT.md` §3.
- **Surprises found:**
  1. Brain HEAD drift: actual `958135d` vs boot-doc `810647d`. PLO-OOF (W1-A P1-P5) already landed.
  2. Chat-only docs are 0-byte on disk; content lives in IDE buffer only.
  3. `CURSOR_HANDOVER.md` is missing entirely (never created). The "8 live-bug classes" are not in the repo.
  4. `docs/PIPELINE_AUDIT.md` already exists (untracked, 28293 B).
  5. 8 modified golden transcripts + ~180 untracked scratch scripts in brain working tree (not touched by this audit).
- **Rules honored:** Zero code changes, zero fixes, zero deploys. Audit/documentation only.
- **Stop:** Do NOT start Phase A2 until the architect signs off.

### CP-W1A — 09 Aug 2026 — Retroactive W1-A Review (audit-only)
- **Status:** [R] (ready for architect review of residual).
- **Scope:** Retroactive review of W1-A (PLO-OOF) commit `958135d`, range `810647d..HEAD` (2 commits: `958135d` PLO-OOF + `c35275b` CP0 docs).
- **(a) Per-commit diff summary (`810647d..HEAD`):**
  - `958135d` "PLO-OOF: PaisaLo out-of-flow completion (P1-P5)" — 23 files, +1709/-42. Engine: `command_gen.py` (+3, committed_date added to FACT_SLOTS_FOR_RESPOND), `executor.py` (+12/-2, G-B6-02 LAST-utter reply_id), `identity_gate.py` (+22/-7, G-B4-01 days_past_due_words derived in slots_for_nlg), `nlg.py` (+13, spoken_days_hindi helper), `scripted_coercions.py` (+145, coerce_callback_request + _extract_committed_date + coerce_committed_date + chain wiring), `tenant_profile.py` (+2, callback_flow field), `tracker.py` (+3, committed_date hydration key). Flows: `paisalo/npa.yml` (G-B3-01 callback capture), `paisalo/objections.yml` (days_past_due_words + digit-by-digit phone), `paisalo/opener.yml` (P2 plo_reask_intent attempt-indexed), `paisalo/postdue.yml` (days_past_due_words), `reply_manifest.json` (slot renames + mandatory flags), `salary_on_time/pre_closure.yml` (P2 sot_push_retry attempt-indexed). Memory: `postgres_borrowers.py` (+2, committed_date mapping), `test_borrower.py` (+2, committed_date key). Tenant: `paisalo.yml` (+128, willing/willing_disqualifiers/callback_request cue packs + coercion_chain + callback_flow). Tests: 6 new golden files (P1-P5 + checkpoint replay), 41 tests.
  - `c35275b` "docs(P0): REPO_CONTEXT.md audit + tracker anchor" — 3 files, +992 (CP0 deliverable, this audit's parent).
- **(b) Full test results for W1-A tests + goldens (Python 3.13.1, pytest 9.1.1):**
  - `test_plo_oof_p1_cue_packs.py` — 15/15 PASSED
  - `test_plo_oof_p2_reask_laddering.py` — 3/3 PASSED
  - `test_plo_oof_p3_grounding_forensic.py` — 5/5 PASSED
  - `test_plo_oof_p4_bside_wins.py` — 8/8 PASSED
  - `test_plo_oof_p5_committed_date.py` — 9/9 PASSED
  - `test_plo_oof_checkpoint_replay.py` — 1/1 PASSED
  - **Total: 41/41 PASSED in 15.77s.**
  - **Environment note:** local default `python` is 3.10.9 which fails at import (`from datetime import UTC` needs 3.11+). Tests require `C:\Users\nitis\AppData\Local\Programs\Python\Python313\python.exe`. Recorded as a dev-env gap (not a code bug).
- **(c) fb6a0f02 t1-t8 replay transcript (`test_cp_fb6a0f02_replay_turn6_advances_to_assurance`):**
  - T1 `""` → `plo_predue_greeting` ✓
  - T2 `"ठीक है।"` → `plo_identity_ask` (identity not yet confirmed) ✓
  - T3 `"ठीक है। हाँ ठीक है, कौन बोल रहे हो?"` → identity confirmed → `plo_reask_intent` ✓
  - T4 `"और कौन सब कह रहे हैं?"` → respond (unknown_info_reply + reask) ✓
  - T5 `"भुगतान कब तक कितना है मेरा?"` → respond (facts SWAPPED → unknown_info_reply + reask) ✓
  - **T6 `"ठीक है।"` → `plo_predue_ack` (assurance) ✓** — P1 willing coercion rescued "ठीक है" to `plo_payment_intent=willing` → assurance path. NO clarify, NO escalation. **Checkpoint assertion met.**
  - T7 `"मैं मैं।"` → clarify (`plo_reask_intent`) — expected (incomplete utterance)
  - T8 `"नहीं नहीं। ये नहीं कितना बहुत काम है।"` → repair_escalation — expected
- **(d) Grounding forensic outcome (P3):**
  - Turn-5 transcript: `"भुगतान कब तक कितना है मेरा?"`
  - Raw LLM respond: `"आपका भुगतान 13-08-2026 तक 4500 rupaye है।"`
  - Guard decision: `grounding_result = "swapped"` (the date `13-08-2026` is NOT in hydrated slots; predue borrower has `dpd=-5`, no `due_date` slot).
  - **Verdict: NEITHER a label bug NOR a swap bypass.** The `"swapped"` label is **accurate** — the facts did NOT speak; the borrower heard the compliance-safe `unknown_info_reply` + the collect re-ask (~176 chars), not the facts + re-ask (~70 chars).
  - **Locking test:** `test_p3_ground_swaps_when_date_not_in_slots` (unit, `ground_respond_text` directly) + `test_p3_turn5_replay_facts_swapped_unknown_reply_spoken` (integration, asserts `grounding_result="swapped"` in the turn_decision log + facts absent from spoken text + unknown_info_reply present).
- **(e) CONFIRMED GAP — PaisaLo reversal stage (H3):**
  - `app/tenants/paisalo.yml:54-60` `coercion_chain: [dispute, callback, willing, refusal, identity, reason_catchall]` — **NO `reversal` entry**.
  - `app/tenants/paisalo.yml:31` `reversal_slots: []` (empty).
  - `app/tenants/paisalo.yml:79` `reversal_target_flow: ""` (empty).
  - No `plo_*reversal*` cue packs, slots, flows, or tests anywhere (recursive grep returned 0 hits).
  - `app/engine/scripted_coercions.py:coerce_commit_reversal` (line 223-260) is a **no-op for PaisaLo**: early-returns at line 230 (`awaiting_slot not in profile.reversal_slots` — always True for `[]`) and at line 258 (`if not target:` — always True for `""`).
  - **H3 was skipped entirely.** No reversal cues/slots landed anywhere for PaisaLo. Recorded as DEBT-016 (W1-A residual). Do NOT fix in A2.
- **(f) W1-A bar:** set to **83%** (5 of 6: P1, P2, P3, P4, P5 done; H3 reversal skipped). Status **[R]**.
- **Rules honored:** Zero code changes, zero fixes, zero deploys. Audit/evidence only.

### CP-A2 — 09 Aug 2026 — Phase A2 Plan (audit-only, not executed)
- **Status:** [P] (planned, blocked on architect sign-off).
- **Deliverable:** `docs/TENANCY_AUDIT.md` — 12 branch points classified (5 PROFILE-FIELD + 6 TEST-SHIM QUARANTINE + 1 LEAK-PATH), 3 live mixing incidents traced (G-A3-01 connector, G-A2-01 brain test-mode, G-A4-03 shared secret), NLG namespace check (YES, a plo_ call can render a sot_ reply_id via force_flow injection — Leak Path A), DT refactor plan (11 new profile fields + 1 guard + 41-orphan deletion list, 1.5d effort).
- **New debt:** DEBT-017..025 (9 new rows). See `docs/TENANCY_AUDIT.md` §5.
- **Stop:** Do NOT execute A2 until the architect signs off.

---

## HARD INVARIANTS (carried from boot doc)

1. **GATE-BEFORE-SIDE-EFFECT.** No warm-transfer, WhatsApp send, or call close before the compliance gate has returned `verdict=allow`.
2. **POLICY PRECEDENCE.** Tenant policy > compliance defaults > engine defaults. Never the reverse.
3. **NO SILENT REWRITES.** Every coercion / LTL rewrite must be logged to the turn decision log.
4. **STATE IS THE SOURCE OF TRUTH.** Borrower record is hydrated into `ConversationState` at session_start; the engine never reads the DB mid-turn.
5. **TEST MODE IS OFF IN UAT.** `TEST_MODE=false` on UAT. Test-mode branches are dev-only.
6. **NO TENANT STRING IN ENGINE.** No `if tenant_id == "..."` in `app/engine/` (W3 target). Currently 12 violations — see `docs/REPO_CONTEXT.md` §3.

---

## KNOWN DEBT REGISTER

See `docs/REPO_CONTEXT.md` §6 for the original 15-item debt register (DEBT-001..DEBT-015). New rows from CP-W1A + CP-A2 (09 Aug 2026):
- **W1-A residual:** DEBT-016 (H3 PaisaLo reversal skipped).
- **A2 (planned):** DEBT-017 (force_flow guard), DEBT-018 (turn.py test-mode borrower factory), DEBT-019 (ws/handler.py test-mode agent_id), DEBT-020 (test_borrower.py loan keys), DEBT-021 (scripted_coercions.py timing slots), DEBT-022 (label_transition.py enforce), DEBT-023 (identity_gate.py bypass flows), DEBT-024 (config.py tenant set), DEBT-025 (41 orphan flows).

Summary by phase:
- **W2-A2:** DEBT-001..005, 007, 008, 010, 012 (config + branch deprecation) + DEBT-017..025 (A2 execution).
- **W1-A residual:** DEBT-016 (H3 reversal).
- **W3:** DEBT-006, 009 (per-tenant isolation).
- **W4:** DEBT-011, 013 (multi-tenant scale + script triage).
- **P0 (user):** DEBT-014, 015 (golden re-record + chat-only doc save).
