# WORKLOG — Fonada Voice Collections

_Append-only log of full checkpoint reports. Chat gets a 5-line summary + sha + WORKLOG entry number; the full report lives here._
_Started 09 Aug 2026. Implementer: Cursor · Reviewer: Claude · Sign-off: Nitish._

---

## Entry #001 — CP0 — P0 Repo Context Audit (09 Aug 2026)

**Status:** [R] → signed off 09 Aug 2026.
**Commit:** `c35275b` "docs(P0): REPO_CONTEXT.md audit + tracker anchor"
**Deliverable:** `docs/REPO_CONTEXT.md` (module map, turn lifecycle walk, complete tenant-conditional inventory, config/env inventory, data model, known-debt appendix).

### HEAD shas confirmed
- brain `collections@feature/tier23-engine-upgrade` HEAD `810647d` (boot doc) → actual scan HEAD `958135d` (PLO-OOF already landed; drift noted).
- go-server `websocket@release/uat-voice-stack` HEAD `4e52063`.
- connector `asterisk-connector@feature/async-ws-audio-writes` `0b7a252`.
- orchestrator `2408ce2`.
- Deployed state: TEST_MODE=false on UAT. Focus tenant: PaisaLo (SOT = test harness only).

### Standing rules (bound by)
NO commit/deploy/live-call without sign-off · every claim carries evidence (log line, test name, stash-proof) · clean builds only, /version verified, no docker-cp, no sed-on-.env · found a bug outside scope → RECORD in tracker, never fix silently · dead air is the worst failure · STOP at every ✋ checkpoint.

### Tenant-conditional counts (regex `sot_|plo_|salary_on_time|paisalo`)
- `app/engine/turn.py`: 78 matches (boot hint ~32; ~12 real branch points, rest are local var names / data).
- `app/engine/nlg.py`: 30 matches (matches hint; all data — slot→reply_id dict entries).
- `app/config.py`: 27 matches (boot hint ~5; 1 real branch at line 610, rest are `Settings` field defaults).
- `app/engine/actions.py`: 70+ matches (action-name dispatch + slot keys — data, not branches).
- `app/engine/scripted_coercions.py`: 7 matches (2 real branches on `profile.flow_prefix`).
- `app/engine/label_transition.py`: 14 matches (1 real branch at line 121, rest are SOT label maps).
- `app/engine/identity_gate.py`: 2 matches (1 hardcoded `sot_opener` flow name).
- `app/ws/handler.py`: 9 matches (3 real branches on `is_paisalo_test` / `test_force`).
- `app/ws/routing.py`: 50+ matches (`FORCE_FLOW_ALIASES` + `ALLOWED_OPENERS` data).
- `app/memory/test_borrower.py`: 12 matches (2 real branches on `state.tenant_id == "paisalo"`).

**Real branch points total: 12.** All 12 are profile-able with new `TenantRuntimeProfile` fields. See `docs/REPO_CONTEXT.md` §3.

### Surprises found
1. Brain HEAD drift: actual `958135d` vs boot-doc `810647d`. PLO-OOF (W1-A P1-P5) already landed.
2. Chat-only docs are 0-byte on disk; content lives in IDE buffer only (later saved to disk in commits `523f1e9` + `39e2404`).
3. `CURSOR_HANDOVER.md` is missing entirely (never created). The "8 live-bug classes" are not in the repo.
4. `docs/PIPELINE_AUDIT.md` already exists (untracked, 28293 B).
5. 8 modified golden transcripts + ~180 untracked scratch scripts in brain working tree (not touched by this audit).

### Rules honored
Zero code changes, zero fixes, zero deploys. Audit/documentation only.

---

## Entry #002 — CP-W1A — Retroactive W1-A Review (09 Aug 2026, audit-only)

**Status:** [R] (ready for architect review of residual).
**Scope:** Retroactive review of W1-A (PLO-OOF) commit `958135d`, range `810647d..HEAD`.
**Commit (this review):** `be23e70` "docs(CP-W1A + CP-A2): retroactive W1-A review + Phase A2 tenancy audit"

### (a) Per-commit diff summary (`810647d..HEAD`)

**`958135d` — "PLO-OOF: PaisaLo out-of-flow completion (P1-P5)"** — 23 files, +1709/-42.
- Engine: `command_gen.py` (+3, `committed_date` added to `FACT_SLOTS_FOR_RESPOND`), `executor.py` (+12/-2, G-B6-02 LAST-utter reply_id), `identity_gate.py` (+22/-7, G-B4-01 `days_past_due_words` derived in `slots_for_nlg`), `nlg.py` (+13, `spoken_days_hindi` helper), `scripted_coercions.py` (+145, `coerce_callback_request` + `_extract_committed_date` + `coerce_committed_date` + chain wiring), `tenant_profile.py` (+2, `callback_flow` field), `tracker.py` (+3, `committed_date` hydration key).
- Flows: `paisalo/npa.yml` (G-B3-01 callback capture), `paisalo/objections.yml` (`days_past_due_words` + digit-by-digit phone), `paisalo/opener.yml` (P2 `plo_reask_intent` attempt-indexed), `paisalo/postdue.yml` (`days_past_due_words`), `reply_manifest.json` (slot renames + mandatory flags), `salary_on_time/pre_closure.yml` (P2 `sot_push_retry` attempt-indexed).
- Memory: `postgres_borrowers.py` (+2, `committed_date` mapping), `test_borrower.py` (+2, `committed_date` key).
- Tenant: `paisalo.yml` (+128, willing/willing_disqualifiers/callback_request cue packs + coercion_chain + callback_flow).
- Tests: 6 new golden files (P1-P5 + checkpoint replay), 41 tests.

**`c35275b` — "docs(P0): REPO_CONTEXT.md audit + tracker anchor"** — 3 files, +992 (CP0 deliverable, this audit's parent).

### (b) Full test results — W1-A tests + goldens

Runner: Python 3.13.1 (`C:\Users\nitis\AppData\Local\Programs\Python\Python313\python.exe`), pytest 9.1.1. (Local default `python` is 3.10.9 and fails at import — `from datetime import UTC` needs 3.11+; recorded as a dev-env gap, not a code bug.)

```
tests/golden/test_plo_oof_p1_cue_packs.py            15/15 PASSED
tests/golden/test_plo_oof_p2_reask_laddering.py         3/3  PASSED
tests/golden/test_plo_oof_p3_grounding_forensic.py       5/5  PASSED
tests/golden/test_plo_oof_p4_bside_wins.py              8/8  PASSED
tests/golden/test_plo_oof_p5_committed_date.py          9/9  PASSED
tests/golden/test_plo_oof_checkpoint_replay.py          1/1  PASSED
                                                       -----
                                              TOTAL:  41/41 PASSED  (15.77s)
```

### (c) fb6a0f02 t1-t8 replay transcript (`test_cp_fb6a0f02_replay_turn6_advances_to_assurance`)

Live PREDUE call (H1-CLOSE redial) turn map, re-run with P1 coercion active:

```
T1 transcript=""                                    -> plo_predue_greeting                         [PASS]
T2 transcript="ठीक है।"                              -> plo_identity_ask (identity not yet confirmed) [PASS]
T3 transcript="ठीक है। हाँ ठीक है, कौन बोल रहे हो?"      -> identity confirmed -> plo_reask_intent       [PASS]
T4 transcript="और कौन सब कह रहे हैं?"                  -> respond (unknown_info_reply + reask)         [PASS]
T5 transcript="भुगतान कब तक कितना है मेरा?"            -> respond (facts SWAPPED -> unknown_info+reask)[PASS]
T6 transcript="ठीक है।"                              -> plo_predue_ack (ASSURANCE)                   [PASS - CHECKPOINT MET]
T7 transcript="मैं मैं।"                               -> clarify (plo_reask_intent)                   [expected - incomplete]
T8 transcript="नहीं नहीं। ये नहीं कितना बहुत काम है।"      -> repair_escalation                             [expected]
```

**T6 "ठीक है" advances to assurance (`plo_predue_ack`). NO clarify, NO escalation.** The P1 willing-cue-pack fix rescued "ठीक है" to `plo_payment_intent=willing` → assurance path. Checkpoint assertion met.

### (d) Grounding forensic outcome (P3) — turn-5 swapped-but-spoke bug

- Turn-5 transcript: `"भुगतान कब तक कितना है मेरा?"`
- Raw LLM respond: `"आपका भुगतान 13-08-2026 तक 4500 rupaye है।"`
- Guard decision: `grounding_result = "swapped"` (the date `13-08-2026` is NOT in hydrated slots; predue borrower has `dpd=-5`, no `due_date` slot).
- **Verdict: NEITHER a label bug NOR a swap bypass.** The `"swapped"` label is **accurate** — the facts did NOT speak; the borrower heard the compliance-safe `unknown_info_reply` + the collect re-ask (~176 chars), NOT the facts + re-ask (~70 chars).
- **Locking tests:**
  - `test_p3_ground_swaps_when_date_not_in_slots` (unit — calls `ground_respond_text` directly)
  - `test_p3_turn5_replay_facts_swapped_unknown_reply_spoken` (integration — asserts `grounding_result="swapped"` in the turn_decision log + facts absent from spoken text + `unknown_info_reply` present)

### (e) CONFIRMED GAP — reversal stage absent from PaisaLo coercion_chain (H3)

Independently verified:
- `app/tenants/paisalo.yml:54-60` — `coercion_chain: [dispute, callback, willing, refusal, identity, reason_catchall]` — **NO `reversal` entry**.
- `app/tenants/paisalo.yml:31` — `reversal_slots: []` (empty).
- `app/tenants/paisalo.yml:79` — `reversal_target_flow: ""` (empty).
- Recursive grep for `plo_*reversal*` across `app/` and `tests/` → **0 hits** (no cue packs, no slots, no flows, no tests).
- `app/engine/scripted_coercions.py:coerce_commit_reversal` (lines 223-260) is a **no-op for PaisaLo**: early-returns at line 230 (`awaiting_slot not in profile.reversal_slots` — always True for `[]`) and at line 258 (`if not target:` — always True for `""`).

**Finding: H3 was skipped entirely. No reversal cues/slots/flows/tests landed anywhere for PaisaLo.** Recorded as **DEBT-016** (W1-A residual). NOT fixed in A2 — separate work item (folded into DT per R3).

### (f) W1-A bar

- **Old:** 60% / `[~]`
- **New:** **83%** / **`[R]`** (5 of 6 sub-items done: P1, P2, P3, P4, P5; H3 reversal skipped → DEBT-016)

### Rules honored
Zero code changes, zero fixes, zero deploys. Audit/evidence only.

---

## Entry #003 — CP-A2 — Phase A2 Tenancy Audit (09 Aug 2026, audit-only)

**Status:** [P] (planned) → **signed off 09 Aug 2026** (architect read `TENANCY_AUDIT.md` from remote).
**Deliverable:** `docs/TENANCY_AUDIT.md` (22837 bytes, commit `be23e70`).
**Scope:** 12 real branch points from `REPO_CONTEXT.md` §3.

### 1. Branch-point classification — 12 real branch points

Buckets: **PROFILE-FIELD** (replace with new `TenantRuntimeProfile` field), **TEST-SHIM QUARANTINE** (test-mode plumbing behind `profile.test_*`), **LEAK-PATH** (guard, not profile field).

| # | File:line | Code | Bucket | Replacement |
|---|---|---|---|---|
| 1 | `app/config.py:610` | `if tenant_id in {"salary_on_time","paisalo"}:` | PROFILE-FIELD | `if get_tenant_profile(tenant_id) is not None` |
| 2 | `app/engine/turn.py:931` | `and request.tenant_id != "paisalo"` | TEST-SHIM | `profile.allow_sot_test_mode` |
| 3 | `app/engine/turn.py:941` | `settings.test_mode and tenant_id=="paisalo" and plo_override` | TEST-SHIM | `profile.test_borrower_factory` |
| 4 | `app/engine/scripted_coercions.py:243` | `profile.flow_prefix != "sot_":` | PROFILE-FIELD | `profile.supports_committed_date_coercion` |
| 5 | `app/engine/scripted_coercions.py:276` | `profile.flow_prefix != "sot_":` | PROFILE-FIELD | `profile.timing_slot_set` |
| 6 | `app/engine/label_transition.py:121` | `if tenant_id == "salary_on_time":` | PROFILE-FIELD | `profile.ltl_enforce_enabled` |
| 7 | `app/engine/identity_gate.py:120` | `"sot_opener" in IDENTITY_FLOWS` | PROFILE-FIELD | `profile.identity_bypass_flows` |
| 8 | `app/ws/handler.py:101` | `is_paisalo_test = ...=="paisalo"` | TEST-SHIM | `profile.test_agent_id` + `test_borrower_id` |
| 9 | `app/ws/handler.py:1211` | `if test_force == "paisalo"` | TEST-SHIM | same as #8 |
| 10 | `app/ws/handler.py:1223` | `(settings.test_tenant_id or "")=="paisalo"` | TEST-SHIM | same as #8 |
| 11 | `app/memory/test_borrower.py:147` | `keys = _PLO_LOAN_KEYS if tenant_id=="paisalo"` | TEST-SHIM | `profile.test_loan_keys` |
| 12 | `app/memory/test_borrower.py:158` | `if state.tenant_id == "paisalo":` | TEST-SHIM | `profile.test_scenario_override_slot` |

**Counts:** PROFILE-FIELD 5 (#1,#4,#5,#6,#7) · TEST-SHIM QUARANTINE 6 (#2,#3,#8-12) · LEAK-PATH 1 (`force_flow` injection — §3). **Total new profile fields: 11** (5 profile + 6 test-shim). **One new guard.**

### 2. Three live mixing incidents (traced)

**Incident 1 — G-A3-01: Connector stamps `client_id=salary-on-time` for PaisaLo ARI calls (P0, connector-owned)**
- Evidence: live PREDUE call `d501f625` `session_start` — top-level `client_id="salary-on-time"`, metadata `client_id="paisalo"` (ignored).
- Path: `asterisk-connector/.../config.go:AudioSocketListener.Tenant` stamps listener → `wsclient.go:sessionStart` fills `client_id` from metadata only when empty (top-level already set, so `paisalo` ignored) → `Websocket/.../brain/client.go:ConfigFromEnv` reads `BRAIN_TENANT_ID` (default `salary-on-time`) → `app/ws/handler.py:handle_brain_websocket` receives `client_id="salary-on-time"`.
- Why UAT "works": Incident 2 masks it.
- Fix class: **LEAK-PATH** (connector/orch fix — out of scope for brain-side A2).

**Incident 2 — G-A2-01: Brain `TEST_MODE`+`TEST_TENANT_ID=paisalo` pins tenant, masking G-A3-01 (P0, brain-owned)**
- Evidence: UAT `.env` has `TEST_MODE=true`, `TEST_TENANT_ID=paisalo`.
- Path: `app/config.py:132` (`test_tenant_id` default) → `app/ws/handler.py:_normalize_test_session_start:101` (`is_paisalo_test` → forces `agent_id="paisalo-test"`, `borrower_id="plo_test_borrower"`) → `handler.py:1211,1223` (`test_force=="paisalo"` / `test_tenant_id=="paisalo"` overrides) → `app/engine/turn.py:926-944` (`sot_override`/`plo_override` test-mode borrower selection).
- Why mixing: brain sees `client_id=salary-on-time` (Incident 1) but `TEST_MODE` forces `tenant_id=paisalo`. In production (`TEST_MODE=false`) the same call would run as `salary_on_time` — a SOT script with a PaisaLo borrower. **Cross-tenant script/borrower mismatch.**
- Fix class: **TEST-SHIM QUARANTINE** — branch points #2,#3,#8-12 collapse into `profile.test_*` fields. **Only one in scope for brain-side A2.**

**Incident 3 — G-A4-03: PaisaLo + SOT share `media_streams` `secret_hash`/hint `ef01` (P2, orch-owned)**
- Evidence: orchestrator `media_streams` SQL dump — `salary-on-time` and `paisalo` rows both have `secret_hash=...ef01...`. PaisaLo SKU = `plo` (nonstandard).
- Path: `ari-orchestrator` reads `media_streams` by tenant for `media_ws_url`+`secret_hash` → shared secret means a media WS client auth'd for SOT can connect to PaisaLo's media endpoint (and vice versa) → `Websocket/.../media/session.go` forwards `client_id` verbatim, can't distinguish tenants by secret alone.
- Fix class: **LEAK-PATH** (orch/DB — rotate PaisaLo `secret_hash` to unique; rename SKU `plo`→standard. Out of scope for brain-side A2).

**Summary:** Only Incident 2 is brain-side A2 scope. Incidents 1 & 3 flagged for connector/orch owners.

### 3. NLG namespace check — can a `plo_` call ever render a `sot_` reply_id?

Walked all 8 resolution paths in `app/engine/nlg.py:draft_reply_resolved` (lines 577-675):

| # | Path | Source | Cross-tenant leak? |
|---|---|---|---|
| 1 | `repeat_reply_id` (591) | `state.slots["repeat_reply_id"]` | YES if a sot_ id set during plo_ call |
| 2 | `utter_chain` (617) | executor utter_chain | YES if sot_ flow on stack |
| 3 | `reply_id` (627) | executor last utter | YES if sot_ flow on stack |
| 4 | `question_slot`→`COLLECT_SLOT_REPLY_IDS` (630) | shared dict lookup | YES if `question_slot` is `sot_*` |
| 5 | `question_slot`+`is_clarify`→`CLARIFY_REASK_REPLY_IDS` (632) | shared dict lookup | YES if `question_slot` is `sot_*` |
| 6 | `clarify_general` (669) | constant | NO — tenant-agnostic |
| 7 | `tenant_cfg.clarify_reply` (650,671,675) | per-tenant text | NO — safe |
| 8 | `tenant_cfg.care_first_reply` (654) | per-tenant text | NO — safe |

**Verdict: YES, a `plo_` call can render a `sot_` reply_id** — via paths 1-5. The leak requires a `sot_*` flow on the call's flow stack. Three ways that happens:

**Leak Path A — `force_flow` injection (the LIVE one).** `app/engine/turn.py:1004-1018` checks `forced_flow in FORCE_FLOW_ALIASES or forced_flow in flows.flows`. `flows.flows` is the GLOBAL FlowSet (all sot_+plo_+generic). **No tenant-catalog check.** A `plo_` call with `force_flow="sot_opener"` injects `sot_opener` → executor walks it → `sot_*` reply_ids + `sot_*` question_slots → NLG renders `sot_*` templates. Same path Incident 2 exploits.

**Leak Path B — LLM `start_flow: sot_*` when `catalog_mode=false`.** `command_gen.py:582-597` rejects out-of-catalog `start_flow` only when `catalog_mode=true`. **Mitigated today:** `turn.py:1097` sets `catalog_mode=true` for all profile tenants. Dormant unless a third tenant is added without a profile or `SCRIPTED_CATALOG_ROUTING=false`.

**Leak Path C — `repeat_reply_id` set by a cross-tenant flow.** No known flow does this today, but no guard prevents it.

**Guard recommendation (audit-only, NOT applied in A2):** add a tenant-catalog check at `turn.py:1007-1009` — `if catalog is None or forced_flow in catalog: state.flow_stack.append(...)`. This is the **one new guard** from §1. Closes Leak Path A. Paths B & C already mitigated by `catalog_mode=true`.

**Conclusion:** The NLG dict itself is NOT at fault — `COLLECT_SLOT_REPLY_IDS`/`CLARIFY_REASK_REPLY_IDS` are shared dicts but the lookup key (`question_slot`) is scoped to the flow on the stack. The leak is upstream (flow-stack injection via `force_flow`), not in NLG. Fallback paths 6-8 are safe.

### 4. DT refactor plan (target ≤ 1.5 days)

**4.1 New `TenantRuntimeProfile` fields (11):** `supports_committed_date_coercion` (bool), `timing_slot_set` (list), `ltl_enforce_enabled` (bool), `identity_bypass_flows` (list), `test_agent_id` (str), `test_borrower_id` (str), `test_borrower_factory` (callable), `test_loan_keys` (list), `test_scenario_override_slot` (str), `allow_sot_test_mode` (bool), implicit `has_tenant_config` (bool computed). See `TENANCY_AUDIT.md` §4.1 for SOT/PLO values + branch-point mapping.

**4.2 One new guard:** `force_flow` tenant-catalog check at `turn.py:1007-1009` (closes NLG Leak Path A → DEBT-017).

**4.3 Deletion list from orphan map** (`scripts/_audit_orphan_map.py`): **41 candidate orphan flows** (12 SOT + 24 PLO + 3 generic). **Caveat:** probe did NOT parse `decide:` branch targets or `escalate_to` targets. Each orphan needs manual verification before deletion. Candidate list, not confirmed list. Deleting these + their `reply_manifest.json` entries reduces flow YAML surface ~30% and reply_manifest ~16% (254→~213).

**4.4 Effort:** 1.5d total (0.25d fields + 0.2d profile-branch replace + 0.3d test-shim quarantine + 0.1d guard + 0.4d orphan verify/delete + 0.25d test re-run).

**4.5 NOT in this DT refactor:** Incident 1 (connector), Incident 3 (orch secret rotation), per-tenant NLG dict isolation (W3), per-tenant borrower tables (W4). DEBT-016 (H3 reversal) folded into DT per R3.

### 5. Register rows (new debt)

**W1-A residual:** DEBT-016 (H3 PaisaLo reversal skipped).

**Phase A2 (planned):** DEBT-017 (force_flow guard), DEBT-018 (turn.py test-mode borrower factory), DEBT-019 (ws/handler.py test-mode agent_id), DEBT-020 (test_borrower.py loan keys), DEBT-021 (scripted_coercions.py timing slots), DEBT-022 (label_transition.py enforce), DEBT-023 (identity_gate.py bypass flows), DEBT-024 (config.py tenant set), DEBT-025 (41 orphan flows).

### 6. Tracker bar updates

| Phase | Old | New | Notes |
|---|---|---|---|
| P0 | 100% [R] | 100% [R] | Signed off 09 Aug 2026 |
| A2 | 0% [ ] | 0% [P] (planned) | `TENANCY_AUDIT.md` delivered; execution blocked on architect sign-off |
| DT | 0% [ ] | 0% [P] (planned) | Folded into A2 (§4) |
| W1-A | 60% [~] | **83% [R]** | P1-P5 done (41/41 pass); H3 skipped → DEBT-016 |

### Rules honored
Zero code changes, zero fixes, zero deploys. Audit/evidence only.

---


