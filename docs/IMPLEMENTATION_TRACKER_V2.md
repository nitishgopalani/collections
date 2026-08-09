# IMPLEMENTATION TRACKER V2 — Fonada Voice Collections

_Phase map + progress bars. Maintained by the implementer._
_This file is the on-disk anchor; the chat-only `Collection/IMPLEMENTATION_TRACKER_V2.md` (0-byte on disk at scan) holds the fuller phase narrative in the IDE buffer and must be merged in by the user._

> **NOTE (P0 audit, 09 Aug 2026):** The chat-only `IMPLEMENTATION_TRACKER_V2.md` at the repo root is 0-byte on disk; its content lives only in the IDE editor buffer. This on-disk copy in `docs/` is the canonical anchor going forward. The user should paste the IDE-buffer content into this file and re-save, then `git add` it.

---

## PHASE MAP

| Phase | Status | Bar | Notes |
|---|---|---|---|
| **P0 — Repo Context Audit** | [R] | 100% | `docs/REPO_CONTEXT.md` written. CP0 signed off 09 Aug 2026. |
| A2 — Tenancy Audit + DT Refactor | [R] | 100% | `docs/TENANCY_AUDIT.md` delivered + signed off 09 Aug 2026. 12 branch points classified, 3 mixing incidents traced, NLG leak path A confirmed, DT refactor plan produced. |
| DT — Decision-Tree Hardening | [R] | **100%** | R1: 10 `TenantRuntimeProfile` fields + `force_flow` catalog guard (DEBT-017) + DEBT-018..024 quarantined. R2: 31 orphan flows deleted (10 ambiguous kept + `ORPHAN(2026-08-09)`). R3: DEBT-016 H3 reversal folded in (paisalo `reversal` stage + `reversal_slots` + `reversal_target_flow=plo_predue` + cue pack + clears `committed_date`; 11 new tests). R4: Invariant #9 — tenant string-compares in `app/engine/`+`app/ws/` 13 -> 6 (all 6 branch-points removed; 6 remaining are non-branch-point data). W1-A goldens 41/41, SOT goldens green, H3 reversal 11/11, full suite parity with baseline (37 pre-existing failures, zero new regressions). See `docs/WORKLOG.md` Entry #004. |
| W1-A — SOT/PLO On-Rails Hardening | [R] | **83%** | P1-P5 done (41/41 tests pass); H3 (PaisaLo reversal stage) cleared via DT R3 (DEBT-016). Bar unchanged at 83% — the H3 residual is now implemented under DT, not W1-A. |
| W1-B — H2 Dead-Air Defense | [R] | **100%** | W1-B.1 ASR reconnect exhausted → `ASREventDead` + `DeadAirHandler` → apology + clean close (go-server). W1-B.2 TTS speak-fail ×2 → holding line → apology + graceful close (recursion-guarded). W1-B.3 `ValidateCarrierRequirements` — carrier=asterisk startup FAILS LOUDLY if ASR_ENABLED/TTS_ENABLED unset. W1-B.4 `reply_empty=<bool> turn_id=<uuid>` structured log on every turn (brain). W1-B.5 14 Go + 3 Python tests, all green; full media suite ok 5.5s. W1-B.6 `apology_dead_air` profile field + PaisaLo draft + fragment library §H candidate #55 PENDING-CLIENT-APPROVAL. **DEBT-026 closed by W1-C C0** (apology text wired brain→go-server via `session_ready` ack; `SetApologyLine` called on the live `*TTSReplyConsumer`). See `docs/WORKLOG.md` Entry #005. |
| W1-C — Policy Interrupts | [R] | **100%** | C0 (DEBT-026) apology text wired brain→go-server: `SessionReadyMessage` carries `apology_text`+`apology_voice_id`; brain client calls `SetApologyLine` on the live `*TTSReplyConsumer` (initial + late paths); simulated ASR-kill → apology audio frames + close + `asr_dead` logged (1 Go + 2 Python tests). Invariant #10 complete. C1 vulnerability lane: `safety_preempt` verified to reach paisalo (cue pack + `care_first_reply`); `_run_safety_early_exit` now tags `disposition=VULNERABLE_FLAGGED` (outcome 5); de-escalation script candidate #56 PENDING-CLIENT-APPROVAL (fragment library §I); 7 tests. C2 DNC/opt-out: `dnc_preempt` policy lane (cue pack `DNC_SIGNALS` + non-committal `policy_stop_calls_reply` — does NOT promise dialer suppression until W4); `_run_dnc_early_exit` tags `disposition=dnc_requested` + graceful END (outcome 7); 9 tests; 2 existing goldens updated (test_compliance_fs4, test_followup_fs6, test_robustness_fs5) for the new preemption. C3 call-window close-out: `call_window_preempt` fires only mid-call (attempts≥1) + outside window → scripted polite close + hangup (outcome 7, `disposition=call_window_closed`); never mid-call silent_reply; 4 tests (incl. fixture with window crossing at turn 3). C4 third-party/speaker-flip guard + DPDP amendment: `third_party_flip_preempt` (cue pack `THIRD_PARTY_FLIP_SIGNALS` + strict/relaxed/open-tier replies); `dpdp_third_party_lock`+`dpdp_disclosure_tier_enforced` profile fields (default strict/true; paisalo.yml defaults); ALWAYS-ON audit logging (`third_party_suspected=true` + `identity_current` transition + `disposition=THIRD_PARTY_FLAGGED`); strict ENDs (outcome 7), relaxed continues; 11 tests (both modes + relaxed log-lines + disclosure LOCK verified). **All 4 preempts run BEFORE Tier-1, always preempt, land in outcome 5/7 with named disposition.** Full golden suite: 401 passed, 1 pre-existing failure (`test_respond_tier3`, pre-DT baseline) — zero new regressions. See `docs/WORKLOG.md` Entry #006. |
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

### CP-PREDUE — 09 Aug 2026 — LIVE PREDUE protocol (partial)
- **Status:** [P] — CALL 1 partial (mechanics healthy, C4 not exercised); CALL 2 NOT made. Brain `e128f41` + go-server `a92239b` deployed on UAT (Nitish-Moh `103.132.145.55:9156`), `/version` + image SHA verified, both healthy.
- **Deploy check:** DONE (PREDUE-1). Silent smoke: DONE via WORKLOG #006 (PREDUE-2, user direction "take 006 instead"). X1 call-window: confirmed open (00:00-23:59 IST, within_call_window=True), no edit needed.
- **CALL 1** (session `ba1c0171-f333-4fe9-b358-cc2406c7b046`, ~59s, 4 turns): mechanics PASS (opener_fallback=false, 8k rates, priya from t1, Tier-3 grounded on probe 3, C0 apology_text_len=255 carried). FAIL: `source=test_force_tenant` (not `client_id` — TEST_FORCE_TENANT override active on UAT); `apology_voice_id=""` (C0 not populated from scenario voice); probe 5 transcript read as refusal (`plo_payment_intent=refused`, not willing); probes 4-6 NOT reached; C4 third-party flip NOT exercised; call ended via `SIM hangup_call` after refusal/push. `mouth_to_ear` 1841ms > 1200ms on t4.
- **CALL 2** (C2 DNC): NOT made.
- **Residual:** (1) unset `TEST_FORCE_TENANT` on brain `.env` (→ `source=client_id`); (2) fix C0 `apology_voice_id` (populate from scenario voice priya); (3) re-run CALL 1 with all 6 probes (keep alive past refusal; Nitish say "ठीक है कर दूंगा" at probe 5); (4) then CALL 2 (C2 DNC). See `docs/WORKLOG.md` Entry #007.
- **Stop:** Awaiting architect direction on next step.

### CP-PREDUE-2 — 09 Aug 2026 — F1/F2/F3/F4 fixes + redeploy + silent smoke + CALL 1 (partial)
- **Status:** [P] — F1/F2/F3/F4 landed + deployed (brain `673f4be`); silent smoke PASS (all 3 criteria); CALL 1 partial (Sarvam ASR died after turn 1 → dead-air apology → close; probes 2-6 NOT reached). CALL 2 NOT made.
- **Fixes landed (brain `673f4be`, pushed `85f6ccd..673f4be`):**
  - F1 (SIM off live path): `hangup_call` action gated behind `TOOLS_HANGUP_SIM` env (default true for lab); `TOOLS_HANGUP_SIM=false` on UAT .env → sim never invoked on live calls. `session_start` log carries `tools_client=<tools_mode>`.
  - F2 (TEST_FORCE_TENANT unset): UAT `.env` `TEST_FORCE_TENANT=paisalo` → `TEST_FORCE_TENANT=` (empty); backup `.env.predue2.bak`. Next call `source=client_id`.
  - F3 (apology_voice_id from scenario voice): `_resolve_plo_scenario_voice(record, settings)` helper in `app/ws/handler.py` (predue/ondue→priya, postdue1/2→neha, postdue3→kabir, npa→amit); used at session_start when `profile.voice_id` empty + tenant=paisalo. 8 new unit tests.
  - F4 (M2E latency debt): DEBT-027 registered.
- **Deploy (18:24 IST):** brain `673f4be` → image `sha256:e14086526c…`, healthy, image-match OK; `.env` post-edit verified (`TEST_FORCE_TENANT=`, `TOOLS_HANGUP_SIM=false`); F1/F3 code presence verified in running container. Go-server unchanged at `a92239b`.
- **Silent smoke (session `ec3f8b88`, 12s):** PASS — `tools_client=simulate` logged, `source=client_id` (F2 worked), `apology_voice_id="priya"` (F3 worked), 8k rates, priya voice, borrower resolved.
- **CALL 1 re-run (session `c890fbf5`, ~44s, 2 turns):** mechanics PASS (opener scripted `plo_predue_greeting` 244 chars, 8k rates, priya, `source=client_id`, identity confirmed on turn 1, latency 933/987ms < 1200ms, no SIM hangup log, preempt stages ran). **FAIL:** Sarvam ASR WebSocket reconnect exhausted after turn 1 → DeadAirHandler fired apology (C0 working — `apology-dead-air` audio seq 49-58) → clean close (`asr_dead`, `asr_errors=2`). Probes 2-6 NOT reached; C4 NOT exercised. **First message correct:** scripted `plo_predue_greeting` (244 chars) overrode LLM's generic help suggestion.
- **CALL 2 (C2 DNC): NOT made.**
- **New debt:** DEBT-028 (Sarvam ASR reconnect exhausted mid-call — BLOCKER for live PREDUE calls; investigate Sarvam API stability / go-server ASR reconnect retry budget before re-running).
- **Residual:** (1) investigate DEBT-028 (Sarvam ASR stability); (2) re-run CALL 1 once ASR stable; (3) then CALL 2 (C2 DNC); (4) revert F2 before production only if live ARI client_id routing unreliable. See `docs/WORKLOG.md` Entry #008.
- **Stop:** Awaiting architect direction on (a) investigate DEBT-028 before re-running, (b) re-run CALL 1 now and hope ASR holds, (c) close PREDUE-2 at partial.

### CP-DEBT028 — 09 Aug 2026 — DEBT-028 diagnosis (D1/D2/D3/D4)
- **Status:** [R] — Root cause confirmed: Sarvam API credits exhausted (billing, not code). D2 hygiene landed (go-server `1a13ef7`). D3 soak + live calls BLOCKED on credits top-up. D4 DEBT-029 registered.
- **D1 forensics (session `c890fbf5`):** ASR WS closed by Sarvam with `close_code=1000 "Insufficient credits"` (18:28:39) then `close_code=1003 "Credits exhausted"` (18:28:40). Nitish was in a ~35s silence gap after probe 1 (not the cause — keepalive ping at 25s held the WS open; Sarvam closed it server-side). Reconnect: 9 dials over ~7s (18:28:39→18:28:46), each fresh WS + fresh `api-key` header + fresh query string (no dead-handshake reuse). Backoff+jitter applied. Verdict: real server-side refusal (credits), not client-side instant-fail.
- **D2 (go-server `1a13ef7`, pushed `a92239b..1a13ef7`):** Verified already-present: keepalive ping 25s, backoff 1s→30s + jitter, fresh handshake per attempt, audio-buffered transparent continue, `close_code`/`close_reason` logged. Landed: `defaultASRMaxReconnects` 5→8 (maxDials 6→9); `reconnect_ms` logged on `sarvam reconnected` / `sarvam reconnect failed` / `sarvam reconnect exhausted`. Tests green; build OK.
- **D3 (soak proof): BLOCKED** — Sarvam credits exhausted; any ASR session dies in ~7s regardless of silence gaps. Gating item: top up Sarvam subscription. After restore: 3-min synthetic session + 20-30s silence gaps → expect zero death or transparent reconnect.
- **D4:** DEBT-029 registered (`tools_client=simulate` serves live UAT; `TOOLS_MODE=live` + `TOOLS_URL` + MPLS access = pre-pilot item; not a PREDUE blocker).
- **Fix for DEBT-028:** TOP UP SARVAM CREDITS (billing/account action — visit Sarvam API Dashboard). No code fix resolves a billing issue. No backup key configured.
- **Stop:** BLOCKED on Sarvam credits. No redial until D3 passes. Awaiting (a) credits top-up, (b) deploy go-server `1a13ef7` + re-run D3, (c) silent smoke → CALL 1 + CALL 2 → WORKLOG #010. See `docs/WORKLOG.md` Entry #009.

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
- **W1-B residual:** DEBT-026 (brain→go-server session_start plumbing for `apology_dead_air` + `voice_id` not yet wired; go-server `DeadAirHandler` + `SetApologyLine` are implemented and unit-tested but `SetApologyLine` is never called in production, so on ASR-dead the handler closes silently without speaking the apology). **CLOSED by W1-C C0 (09 Aug 2026):** `SessionReadyMessage` now carries `apology_text`+`apology_voice_id`; brain client calls `SetApologyLine` on the live `*TTSReplyConsumer` on session_ready (initial + late paths). Invariant #10 complete.
- **W1-C (09 Aug 2026):** No new debt rows. C2 intentionally does NOT set `dunning_suppressed` (W4 dialer work — promising suppression now would be a lie); the `dnc_requested` audit flag is enough for this release. C4 DPDP posture is brand-configurable via `dpdp_third_party_lock`+`dpdp_disclosure_tier_enforced` (paisalo.yml defaults to strict/true until the brand says otherwise). C1 de-escalation script + C4 third-party scripts are PENDING-CLIENT-APPROVAL (fragment library candidates #56 + §I/§J).
- **PREDUE-007 (09 Aug 2026):** DEBT-027 (M2E latency budget breach — live CALL 1 turn t4 `mouth_to_ear latency budget exceeded 1841ms > 1200ms`). Not a blocker (call completed, no functional impact); tracked as latency debt. Root cause TBD — likely LLM respond + TTS synthesis serial path; investigate under W2 evidence-scorer work or a dedicated latency sprint.
- **PREDUE-008 (09 Aug 2026):** DEBT-028 (Sarvam ASR WebSocket reconnect exhausted mid-call — live CALL 1 session `c890fbf5` died after turn 1 at ~44s, triggering W1-B H2 dead-air apology + clean close; probes 2-6 never reached). **BLOCKER for live PREDUE calls.** D1 root cause: Sarvam closed the WS with `close_code=1000 "Insufficient credits"` then `close_code=1003 "Credits exhausted. Visit the API Dashboard to review and manage your subscription."` — **the Sarvam API account is out of credits** (billing issue, NOT a network blip and NOT a go-server bug). The reconnect logic was already correct (fresh handshake each attempt, exponential backoff + jitter, keepalive ping, audio-buffered transparent continue). D2 hygiene bumps landed (go-server `1a13ef7`): `defaultASRMaxReconnects` 5→8 (maxDials 6→9); `reconnect_ms` now logged on `sarvam reconnected` / `sarvam reconnect failed` / `sarvam reconnect exhausted`. **Fix: top up the Sarvam API subscription (billing/account action).** D3 soak test + live calls BLOCKED until credits are topped up.
- **PREDUE-008 (09 Aug 2026):** DEBT-029 (`tools_client=simulate` still serves live UAT calls — `TOOLS_MODE=simulate` in UAT `.env`; the `sot_tools_sim` hangup is gated by `TOOLS_HANGUP_SIM=false` (F1, acceptable interim), but the LLM tool-calling client (`app.state.tools` = `FakeToolClient`) is still the simulate mode. `TOOLS_MODE=live` requires the real tool backend (`LiveToolClient` → `TOOLS_URL`) + MPLS access confirmation — **pre-pilot item**. Not a blocker for PREDUE (scripted coercion flows don't exercise LLM tool-calling), but must be resolved before pilot launch. See `app/clients/tools_live.py` "DECISION NEEDED: confirm real tool contract + MPLS access before TOOLS_MODE=live".

Summary by phase:
- **W2-A2:** DEBT-001..005, 007, 008, 010, 012 (config + branch deprecation) + DEBT-017..025 (A2 execution).
- **W1-A residual:** DEBT-016 (H3 reversal).
- **W3:** DEBT-006, 009 (per-tenant isolation).
- **W4:** DEBT-011, 013 (multi-tenant scale + script triage).
- **P0 (user):** DEBT-014, 015 (golden re-record + chat-only doc save).

---

## KNOWN-RED TEST FAILURES (C2, 09 Aug 2026)

_Full-suite (`tests/golden` + `tests/unit`, Python 3.13.1, pytest 9.1.1, `--tb=no -p no:cacheprovider`): **37 failed / 787 passed / 5 skipped** — exact parity with pre-DT baseline (`4663bdf`). All 37 classified pre-existing; **zero new regressions from DT.**_

**Classification (37 = 29 pollution + 8 remainder):**

| # | Test | Class | Proof |
|---|---|---|---|
| 1-29 | (the 29 test-order pollution set) | **TEST-ORDER POLLUTION** | `scripts/_p6_f2_failures.txt` + `scripts/_h1_failure_diff.txt` (SHARED fails both, 29). All `lifespan SystemExit` / `startup_validation` env pollution under full-suite order; pass in isolation and in sub-groups. Files: `test_moderator_history_fix` (2), `test_phase_c_multitenancy` (7), `test_prompt_streaming` (3), `test_prompt_ws_integration` (15), `test_tools_sprint3` (1), `test_ws_streaming` (1). |
| 30 | `test_respond_tier3::test_reason_given_after_respond_advances_push` | **PRE-EXISTING (content)** | Fails on pre-DT baseline `4663bdf` (stash + run). `last_question_slot` assertion mismatch — unrelated to DT. |
| 31 | `test_flowset_caching::test_handle_turn_does_not_call_load_all_flows_when_cache_warm` | **PRE-EXISTING (content)** | Fails on pre-DT baseline `4663bdf` (stash + run). `MissingSlotError: customer_name` — unrelated to DT. |
| 32 | `test_live_kb::test_live_retrieve_promise_to_pay` | **PRE-EXISTING (live network)** | Live KB test — needs live API keys; normally `--ignore`d (see `_p6_f2_failures.txt` header "live_* ignored"). |
| 33 | `test_live_kb::test_live_healthz_kb_live_llm_live_tools_stub` | **PRE-EXISTING (live network)** | Live KB test — same as #32. |
| 34 | `test_live_vertex::test_live_kal_paisa_de_dunga_ptp` | **PRE-EXISTING (live network)** | Live Vertex test — needs live Vertex API; normally `--ignore`d. |
| 35 | `test_live_vertex::test_live_multi_signal_dispute_and_ptp` | **PRE-EXISTING (live network)** | Live Vertex test — same as #34. |
| 36 | `test_live_vertex::test_live_healthz_llm_live_kb_tools_stub` | **PRE-EXISTING (live network)** | Live Vertex test — same as #34. |
| 37 | `test_manifest_locks::test_committed_manifest_matches_generator` | **PRE-EXISTING (content)** | Fails on pre-DT baseline `4663bdf` (stash + run, 09 Aug 2026). Committed manifest vs generator drift — pre-existing; not caused by DT orphan deletion (manifest is keyed by reply_id, not flow name; deleted orphans had zero manifest refs per R2 verify). |

**Verdict:** All 37 pre-existing. No failure requires reporting before W1-B checkpoint. The 29 pollution set is tracked as rolling debt (`MASTER_SPRINT_PLAN` §3 Debt row). The 8 remainder: 2 content + 5 live-network + 1 manifest-drift — all pre-existing, none introduced or worsened by DT.

