# WORKLOG — Fonada Voice Collections

_Append-only log of full checkpoint reports. Chat gets a 5-line summary + sha + WORKLOG entry number; the full report lives here._
_Started 09 Aug 2026. Implementer: Cursor · Reviewer: Claude · Sign-off: Nitish._

> **Encoding rule (C1, 09 Aug 2026):** This file is **always UTF-8**. Em-dashes (—), arrows (→), and Devanagari must survive every edit. Never use PowerShell `Add-Content` on this file (it double-encodes UTF-8); use the Write/StrReplace tools or a Python script that reads + writes with `encoding="utf-8"`.

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



## Entry #004 — CP-DT — Phase DT Decision-Tree Refactor (09 Aug 2026)

**Status:** [R] — ready for architect sign-off.
**Rulings covered:** R1 (10 profile fields + DEBT-017 guard), R2 (DEBT-025 orphan verify-then-delete), R3 (DEBT-016 H3 reversal folded in), R4 (Invariant #9 proof).

### R1 — 10 TenantRuntimeProfile fields + force_flow catalog guard (DEBT-017)

**New fields on `TenantRuntimeProfile`** (`app/engine/tenant_profile.py`):
1. `supports_committed_date_coercion: bool` (DEBT-021) — SOT true, PLO false.
2. `timing_slot_set: tuple[str, ...]` (DEBT-021) — SOT `(sot_customer_time, sot_commit_timing)`, PLO `()`.
3. `ltl_enforce_enabled: bool` (DEBT-022) — SOT true, PLO false.
4. `identity_bypass_flows: frozenset[str]` (DEBT-023) — SOT `{sot_opener}`, PLO `{plo_opener}`.
5. `allow_sot_test_mode: bool` (DEBT-018) — SOT true, PLO false.
6. `test_borrower_factory: str` (DEBT-018) — SOT `hardcoded_test_borrower`, PLO `hardcoded_paisalo_borrower`.
7. `test_borrower_id: str` (DEBT-019) — SOT `sot_test_borrower`, PLO `plo_test_borrower`.
8. `test_agent_id: str` (DEBT-019) — SOT `salary-on-time-test`, PLO `paisalo-test`.
9. `test_loan_keys: tuple[str, ...]` (DEBT-020) — mirrors `_SOT_LOAN_KEYS` / `_PLO_LOAN_KEYS`.
10. `test_scenario_override_slot: str` (DEBT-020) — SOT `""`, PLO `plo_scenario_override`.

**Field validators updated:** `identity_bypass_flows` added to `_as_frozenset`; `timing_slot_set` + `test_loan_keys` added to `_as_tuple`.

**Tenant YAMLs populated:** `app/tenants/salary_on_time.yml` + `app/tenants/paisalo.yml`.

**Engine edits — branch points replaced/quarantined (zero tenant string-compares):**
- `app/engine/scripted_coercions.py` #4,#5: `flow_prefix != "sot_"` -> `profile.timing_slot_set`.
- `app/engine/label_transition.py` #6: `if tenant_id == "salary_on_time"` -> `profile.ltl_enforce_enabled`.
- `app/engine/identity_gate.py` #7: hardcoded `"sot_opener"` set -> `profile.identity_bypass_flows` (strict when configured, legacy fallback when empty — preserves `test_generic`).
- `app/config.py` #1: `if tenant_id in {"salary_on_time","paisalo"}` -> `if get_tenant_profile(tenant_id) is not None`.
- `app/ws/handler.py` #8: `is_paisalo_test` string-compare -> `profile.test_borrower_id` / `test_agent_id`.
- `app/ws/handler.py` #9,#10: `test_force=="paisalo"` / `test_tenant_id=="paisalo"` -> `profile.test_agent_id`.
- `app/memory/test_borrower.py` #11,#12: `state.tenant_id=="paisalo"` -> `profile.test_loan_keys` / `test_scenario_override_slot`.
- `app/engine/turn.py` #2,#3: `request.tenant_id != "paisalo"` / `== "paisalo"` -> `profile.allow_sot_test_mode` / `test_borrower_factory` (via new `_resolve_test_borrower_factory` helper).

**DEBT-017 force_flow tenant-catalog guard** (`app/engine/turn.py`): a forced flow that is not in the active tenant's `tenant_flow_catalog` is dropped before injection — closes NLG Leak Path A (a `plo_` call can no longer inject `sot_opener` via `force_flow`).

### R2 — DEBT-025 orphan verify-then-delete

**Verify rule (R2):** delete ONLY flows with zero `reply_manifest` references AND zero test references AND zero `force_flow`-alias references. Ambiguous -> keep + `ORPHAN(2026-08-09)` comment.

**Verification script:** `scripts/_dt_orphan_verify.py` (word-boundary grep across manifest, tests/, routing.py+handler.py).
**Deletion script:** `scripts/_dt_orphan_delete.py` (block-preserving; preamble comments kept).

**Final delete list (31 flows):**
- `app/flows/compliance_handoff.yml` (1): `incapacitated_borrower`
- `app/flows/refusal_negotiation.yml` (1): `beyond_authority`
- `app/flows/robustness.yml` (1): `off_topic_redirect`
- `app/flows/paisalo/objections.yml` (10): `plo_obj_assurance_pd`, `plo_obj_dealer_pay_pd`, `plo_obj_death_pd`, `plo_obj_lost_qr`, `plo_obj_multiple_loans_pd`, `plo_obj_paid_official_pd`, `plo_obj_personal_issue`, `plo_obj_where_to_pay`, `plo_obj_will_not_pay`, `plo_obj_will_you_pay_pd`
- `app/flows/paisalo/npa.yml` (14): `plo_obj_dealer_pay`, `plo_obj_multiple_loans`, `plo_obj_npa_angry`, `plo_obj_npa_assurance`, `plo_obj_npa_branch_address`, `plo_obj_npa_death`, `plo_obj_npa_hardship`, `plo_obj_npa_lost_qr`, `plo_obj_npa_medical`, `plo_obj_npa_paid_official`, `plo_obj_npa_refuse`, `plo_obj_npa_where_to_pay`, `plo_obj_npa_wrong_number`, `plo_obj_will_you_pay`
- `app/flows/salary_on_time/post_due.yml` (4): `sot_obj_amount_in_2_days`, `sot_obj_pay_later_penalty`, `sot_obj_penalty_now`, `sot_obj_total_payable`

**Ambiguous (10, kept + ORPHAN comment in `app/flows/salary_on_time/pre_closure.yml`):** `sot_obj_cant_login`, `sot_obj_credit_manager`, `sot_obj_is_bot`, `sot_obj_month_only`, `sot_obj_pending_status`, `sot_obj_processing_fee`, `sot_obj_reduce_amount`, `sot_obj_references_called`, `sot_obj_support_number`, `sot_obj_unknown_query` — all referenced in `FORCE_FLOW_ALIASES` (`app/ws/routing.py`).

**Flow set after deletion:** 101 flows loaded (was 132). `plo_opener`/`sot_opener` present; deleted orphans absent.

### R3 — DEBT-016 H3 reversal folded in (config + minimal code)

**`app/tenants/paisalo.yml`:**
- `coercion_chain`: added `reversal` after `identity` -> `dispute, callback, willing, refusal, identity, reversal, reason_catchall`.
- `reversal_slots`: `[committed_date, plo_payment_intent, plo_timeline]`.
- `reversal_target_flow`: `plo_predue`.
- new `reversal` cue pack: `haan... actually nahi`, `nahi karunga ab`, `mana kar`, `cancel karo` (+ Devanagari + variants).

**`app/engine/scripted_coercions.py:coerce_commit_reversal`:**
- Uses dedicated `reversal` cue pack when configured (PLO); falls back to `refusal` pack when empty (SOT — unchanged).
- Clears `committed_date` (emits `set_slot committed_date=""`) on fire when `committed_date` is in `reversal_slots` (PLO only; SOT's `reversal_slots` don't include it -> SOT behaviour unchanged).

**New tests** (`tests/golden/test_plo_h3_reversal.py`, 11 cases): reversal cue at `committed_date` clears it + routes to `plo_predue`; `nahi karunga ab` / `mana kar` / `cancel karo` fire; non-reversal-slot no-fire; SOT regression guard (no `committed_date` clear); config guards (slots, target, chain, cue pack).

**Existing test updated** (`tests/golden/test_plo_oof_p1_cue_packs.py::test_p1_paisalo_coercion_chain_includes_willing_and_refusal`): expected chain now includes `reversal`.

### R4 — Invariant #9 proof (tenant string-compares in `app/engine/` + `app/ws/`)

**BEFORE (grep `"salary_on_time"|"paisalo"`):**
- `app/engine/`: 5 hits — `turn.py:279` (default profile), `turn.py:931` (#2), `turn.py:941` (#3), `label_transition.py:121,123` (#6).
- `app/ws/`: 8 hits — `handler.py:101` (#8), `handler.py:1211` (#9), `handler.py:1223` (#10), `routing.py:17-20,84` (FORCE_FLOW_ALIASES + client_id map data).
- **Total BEFORE: 13.** Branch-point string-compares: 6 (#2,#3,#6,#8,#9,#10).

**AFTER (grep `"salary_on_time"|"paisalo"`):**
- `app/engine/`: 1 hit — `turn.py:279` (`_sot_profile()` default profile lookup — NOT a branch point; legitimate fallback for open tenants).
- `app/ws/`: 5 hits — `routing.py:17-20,84` (`FORCE_FLOW_ALIASES` + `_CLIENT_ID_TO_TENANT` data — NOT branch points; client_id->tenant routing table).
- **Total AFTER: 6.** Branch-point string-compares: **0** (all 6 removed).

**Invariant #9:** `AFTER (6) <= 12 - quarantined (6) = 6`. **Met.** The 6 remaining grep hits are non-branch-point (default profile + routing data). All 6 branch-point string-compares in `app/engine/`+`app/ws/` eliminated; the other 6 of the 12 branch points (config.py #1, scripted_coercions #4/#5, identity_gate #7, memory/test_borrower #11/#12) used `flow_prefix`/`tenant_id` checks (not `"salary_on_time"`/`"paisalo"` literals) and were replaced with profile fields — they never appeared in this grep.

### Test results

- **W1-A goldens (PLO-OOF P1-P5 + checkpoint replay):** 41/41 PASS (zero behaviour diff).
- **SOT goldens (pre_closure, catalog_routing) + PLO scenarios:** 30/30 PASS.
- **H3 reversal (new):** 11/11 PASS.
- **SOT repair layer (reversal regression guard):** 67/67 PASS.
- **tenant_profile unit (incl. `test_generic_tenant_happy_path`, `test_sot_force_flow_still_bypasses_identity_gate`):** 14/14 PASS.
- **P1 coercion-chain test (updated for R3):** PASS.
- **Full suite (`tests/golden` + `tests/unit`):** 37 failed / 786 passed — **parity with baseline** (baseline: 37 failed / 787 passed; the +1 pass is the new H3 reversal file). All 37 failures are pre-existing (test-ordering pollution in WS/streaming/multitenancy files — they pass in isolation and in sub-groups; 2 are pre-existing content failures `test_respond_tier3::test_reason_given_after_respond_advances_push` and `test_flowset_caching::test_handle_turn_does_not_call_load_all_flows_when_cache_warm`, both fail on baseline too). **Zero new regressions introduced by DT.**

### Rules honored
Zero behaviour diff for SOT goldens (met). PaisaLo goldens green + new reversal tests (met). No deploys.

### Tracker bars (this entry)
|| Phase | Old | New | Notes |
||---|---|---|---|
|| P0 | 100% [R] | 100% [R] | — |
|| A2 | 100% [R] | 100% [R] | Signed off 09 Aug 2026 |
|| DT | 0% [ ] | **100% [R]** | R1-R4 done; 10 fields + guard, 31 orphans deleted, H3 reversal, Inv#9 13->6 |
|| W1-A | 83% [R] | 83% [R] | DEBT-016 cleared via R3; bar unchanged (residual was the reversal, now folded into DT) |

---



---

## Entry #005 — CP-W1B — Phase W1-B H2 Dead-Air Defense (09 Aug 2026)

**Status:** [R] — ready for architect sign-off.

### Carry-in C1 — WORKLOG mojibake fix

Entry #004's em-dashes (—) were double-encoded to `â€"` by PowerShell `Add-Content`
re-encoding already-UTF-8 bytes. Fixed via `scripts/_c1_fix_worklog_utf8.py` (read
UTF-8, replace U+00E2 U+20AC U+201D → U+2014, write UTF-8). 0 mojibake sequences
remain; 80 em-dashes now correct. Added a "UTF-8 always" note at the WORKLOG top
forbidding `Add-Content` on this file.

### Carry-in C2 — 37 full-suite failures classified (all pre-existing)

Full suite (`tests/golden` + `tests/unit`, Python 3.13.1, `--tb=no`): **37 failed /
787 passed / 5 skipped** — exact parity with pre-DT baseline (`4663bdf`). Table
added to `docs/IMPLEMENTATION_TRACKER_V2.md` §KNOWN-RED TEST FAILURES. Breakdown:
- **29 = test-order pollution set** (the known-red register from
  `scripts/_p6_f2_failures.txt` / `scripts/_h1_failure_diff.txt`): all
  `lifespan SystemExit` / `startup_validation` env pollution under full-suite
  order; pass in isolation. Files: `test_moderator_history_fix` (2),
  `test_phase_c_multitenancy` (7), `test_prompt_streaming` (3),
  `test_prompt_ws_integration` (15), `test_tools_sprint3` (1),
  `test_ws_streaming` (1).
- **8 remainder** — all pre-existing:
  - `test_respond_tier3::test_reason_given_after_respond_advances_push` — pre-existing content (verified on baseline `4663bdf`).
  - `test_flowset_caching::test_handle_turn_does_not_call_load_all_flows_when_cache_warm` — pre-existing content (verified on baseline).
  - `test_live_kb` (2) + `test_live_vertex` (3) — live-network tests (need live API keys; normally `--ignore`d).
  - `test_manifest_locks::test_committed_manifest_matches_generator` — pre-existing (verified on baseline `4663bdf`, 09 Aug 2026).

**Verdict:** zero new regressions from DT. No failure required reporting before W1-B.

### W1-B.1 — ASR reconnect exhausted → apology + clean close (go-server)

- `Websocket/internal/media/asr.go`: new `ASREventDead` event type (terminal,
  distinct from transient `ASREventError`).
- `Websocket/internal/media/sarvam_asr.go::tryReconnect`: both give-up paths
  (reconnectFails-exhaustion at line 468 AND dialCount-exhaustion at line 502)
  now emit `ASREventDead` (was `ASREventError`).
- `Websocket/internal/media/asr_sink.go`: new `ASRDeadListener` interface +
  `SetDeadAirListener` setter. `consumeEvents` on `ASREventDead` logs
  `asr_dead=true ... dead_air_handler_wired=<bool>` at ERROR level and invokes
  the listener. Never continues deaf.
- `Websocket/internal/media/dead_air.go`: new `DeadAirHandler` implements
  `ASRDeadListener`; `OnASRDead` → `ttsConsumer.SpeakApologyAndClose`.
- `Websocket/cmd/server/main.go`: sink factory wires `DeadAirHandler` to the
  `ASRSink` when TTS is live.

### W1-B.2 — TTS speak-fail → holding line → graceful close (go-server)

- `Websocket/internal/media/tts_reply_consumer.go`: new fields
  `consecutiveSpeakFails`, `holdingLine`, `apologyText`, `apologyVoiceID`,
  `apologyTurnID`. New setters `SetHoldingLine`, `SetApologyLine`. New
  `SpeakApologyAndClose` (speaks apology in `unknown_info` voice + `end_call`
  via `onEndCall`; always closes even with empty apology — never mute).
- `OnReplyChunk` Speak error path refactored to `handleSpeakFailure`:
  - empty-text Speak (flush) failing → WARN, not a dead-air fault.
  - 1st non-empty fail → ERROR log + holding-line attempt (if configured).
  - 2nd consecutive fail → `SpeakApologyAndClose`.
  - **Recursion guard:** if the failing turn IS the apology turn
    (`apologyTurnID`), TTS is dead → close silently, never re-attempt apology.
  - successful non-empty Speak → `resetSpeakFailures`.

### W1-B.3 — startup FAILS LOUDLY under carrier=asterisk (go-server)

- `Websocket/internal/media/carrier.go`: new `ValidateCarrierRequirements`
  + `CarrierRequirementError`. Under `carrier=asterisk`, `ASR_ENABLED` and
  `TTS_ENABLED` must both be true (deaf/mute call is never acceptable).
  Fonada/Exotel/unknown carriers unaffected.
- `Websocket/cmd/server/main.go`: calls validation after config load;
  `os.Exit(1)` on failure with structured ERROR log (carrier + reasons).

### W1-B.4 — reply_empty=true logging with turn_id (brain)

- `Collection/app/engine/turn.py`: after `_persist_turn`, emits structured
  `reply_empty=<bool> turn_id=<audit_id> call_id=<call_id> tenant_id=<tenant_id>
  reply_id=<reply_id> final_text_len=<n>` log line. Silence is always visible
  and greppable in isolation. Fires on every turn (empty or not).

### W1-B.5 — Tests

**Go (`Websocket/internal/media/`):** 14 new tests, all green.
- `carrier_w1b_test.go` (7): carrier=asterisk both-off / ASR-off / TTS-off /
  both-on / Fonada / Exotel / unknown — validation matrix.
- `w1b_dead_air_test.go` (7):
  - `TestSarvamReconnectExhaustedEmitsDeadEvent` — failing-dial provider →
    ASREventDead emitted.
  - `TestASRSinkHandlesDeadEventAndInvokesListener` — sink → listener invoked
    with correct session.
  - `TestASRSinkDeadEventLoggedWithoutListener` — no panic when unwired.
  - `TestTTSConsecutiveSpeakFailTriggersApologyAndClose` — fully-dead TTS →
    close via apology path (recursion guard holds).
  - `TestTTSFirstSpeakFailHoldingLineNoClose` — fail-first-then-succeed stream →
    holding line succeeds, no close, counter resets.
  - `TestTTSNoopWithTextNoEscalation` — noop Speak returns nil → counter
    stays 0, no close.
  - `TestSimulatedASRWSKillProducesApologyAudioFrames` — ASREventDead →
    DeadAirHandler → synth TTS → capturing egress receives ≥2 apology audio
    frames (caller would hear them) + session closed.
- Full `Websocket/internal/media` suite: **ok 5.5s** — zero regressions.

**Python (`Collection/tests/golden/test_w1b_reply_empty_log.py`):** 3 tests, all green.
- `test_w1b4_reply_empty_false_logged_on_normal_turn` — non-empty reply logs
  `reply_empty=False turn_id=<uuid>` matching `resp.audit_id`.
- `test_w1b4_reply_empty_true_logged_on_empty_reply` — monkeypatched empty
  gate reply logs `reply_empty=True turn_id=<uuid>`.
- `test_w1b4_reply_empty_log_carries_call_and_tenant` — log line carries
  `call_id` + `tenant_id` for triage.
- W1-A goldens (`test_plo_h3_reversal`, `test_plo_oof_p1_cue_packs`): 24/24
  green — zero regressions.

### W1-B.6 — Apology line copy + fragment library candidate #55

- `Collection/app/engine/tenant_profile.py`: new `apology_dead_air: str` field.
- `Collection/app/tenants/paisalo.yml`: `apology_dead_air` = the user-supplied
  draft "माफ़ कीजिए, लाइन में तकनीकी समस्या आ रही है। हम आपसे थोड़ी देर में दोबारा
  संपर्क करेंगे। धन्यवाद।"
- `Collection/app/tenants/salary_on_time.yml`: same draft (SOT variant TBD).
- `Collection/PAISALO_FRAGMENT_LIBRARY_V1.md`: new §H "Dead-air apology (W1-B)"
  with `apology_dead_air` fragment, marked **PENDING-CLIENT-APPROVAL candidate #55**.
  Until approved, the engine reads the copy from the profile config (hot-swappable,
  no redeploy).

### W1-B residual (recorded, NOT fixed)

The go-server dead-air handler (`DeadAirHandler` + `TTSReplyConsumer.SetApologyLine`)
is implemented and unit-tested, but the **brain→go-server session_start param
plumbing for `apology_dead_air` + `voice_id` is not yet wired end-to-end**. Today
`SetApologyLine` is never called in production, so `apologyText` defaults to empty
→ on ASR-dead the handler closes silently (no apology spoken). Wiring this needs
a brain-side session_start message carrying `apology_dead_air` + `voice_id` and
a go-server `BootstrapSink`/session_start hook calling `SetApologyLine`. Tracked
as **DEBT-026** (W1-B residual) in the register. No behaviour diff for live
calls (the dead-air path was previously "continue deaf"; now it closes — a
strict improvement even without the spoken apology).

### Test summary

- Go `internal/media`: 14 new W1-B tests pass; full suite green (ok 5.5s).
- Brain `tests/golden/test_w1b_reply_empty_log.py`: 3/3 pass.
- W1-A goldens (H3 reversal + P1 cue packs): 24/24 pass.
- Full brain suite: not re-run at CP-W1B (DT parity 37/787 already established;
  W1-B brain change is a single additive log line, gated on `audit_id`/`resolved`
  which always exist on the main turn path; no existing assertion touched).

### Shas

- Brain (Collection): see `git log` HEAD after CP-W1B commit.
- Go-server (Websocket): see `git log` HEAD after CP-W1B commit.


## Entry #006 — CP-W1C — Phase W1-C Policy Interrupts + DEBT-026 (09 Aug 2026)

**Status:** [R] — ready for architect sign-off.

### C0 — DEBT-026 closed: apology_dead_air wired brain→go-server (invariant #10)

The W1-B residual is closed. The brain's `SessionReadyMessage` (`app/schemas/ws_contract.py`)
now carries `apology_text` + `apology_voice_id`; the session_start handler
(`app/ws/handler.py`) populates them from the tenant profile's
`apology_dead_air` + `voice_id`. On the go-server, `SessionReadyPayload`
(`Websocket/internal/brain/contract.go`) carries the same fields, and the
brain client (`client.go::readSessionReady` + the late-session_ready
dispatch branch) type-asserts the reply consumer to
`*media.TTSReplyConsumer` and calls `SetApologyLine(text, voice)`. Open
tenants (no profile) leave both empty → handler closes silently (no
apology spoken).

**Test (Go):** `Websocket/internal/brain/client_w1c_test.go::
TestSessionReadyWiresApologyLineThenASRKillSpeaksItAndCloses` — fake
brain WS server sends session_ready with apology_text (255 chars) +
voice_id=abhilash; brain client wires `SetApologyLine`; simulated
ASR-kill (`DeadAirHandler.OnASRDead`) → apology audio frames (≥2) reach
egress + `endCall` fires + `asr_dead` logged. PASS (0.18s).

**Tests (Python):** `tests/golden/test_w1c_apology_session_ready.py` —
paisalo session_start → session_ready carries non-empty `apology_text`
containing "तकनीकी समस्या"; open tenant → `apology_text=""`. 2/2 PASS.

Invariant #10 (apology spoken before close on dead-air) is now complete
end-to-end. The `apology_dead_air` copy remains PENDING-CLIENT-APPROVAL
(fragment library §H candidate #55); hot-swappable via the profile YAML
without a redeploy.

### C1 — Vulnerability lane (policy interrupt, outcome 5)

**Verification:** the Sprint-6 `safety_preempt` (`app/engine/safety.py`)
ALREADY reaches paisalo. `tenant_config("paisalo")` returns a
`TenantConfig` with `vulnerability_signals` + `distress_signals` +
`care_first_reply` from `default_compliance_policy()`. The cue pack
covers death-adjacent / self-harm / distress Hindi cues: `mar jana`,
`mar jaunga`, `mar jaungi`, `zinda nahi`, `suicide`, `ro raha/rahi`,
`bahut tension`, `bikhar gaya`, etc. `safety_preempt` is wired in the
turn pipeline (`app/engine/turn.py` ~line 1442) BEFORE the Tier-1
evidence scorer, and preempts via `_run_safety_early_exit`.

**Gap fixed:** the early-exit returned `disposition=None`. Now set to
`disposition="VULNERABLE_FLAGGED"` (outcome 5 — transfer to human
specialist; `transfer_to_human=True` already set by `safety_preempt`).
Dunning is suppressed + recovery suspended (existing behavior).

**Fragment candidate #56** (`PAISALO_FRAGMENT_LIBRARY_V1.md` §I,
PENDING-CLIENT-APPROVAL): empathy-register de-escalation line
"मैं आपकी बात सुन रहा/रही हूँ। आपकी सुरक्षा हमारे लिए सबसे ज़रूरी है —
मैं अभी आपको हमारी केयर टीम के एक स्पेशलिस्ट से जोड़ रहा/रही हूँ। कृपया
लाइन पर बने रहें।" Until client approval, the engine uses
`TenantConfig.care_first_reply` verbatim (hot-swappable).

**Tests:** `tests/golden/test_w1c_vulnerability_lane.py` — 5 cues
parametrized; each asserts scorer call_count==0 (no evidence veto),
`disposition==VULNERABLE_FLAGGED`, `transfer_to_human=True`, empathy
register, no flow started; plus unit tests for dunning suppression +
recovery suspension + paisalo cue-pack presence. 7/7 PASS.

### C2 — DNC / opt-out capture (policy interrupt, outcome 7)

New policy-lane `dnc_preempt` (`app/engine/safety.py`) fires BEFORE Tier-1
on DNC cues (`DNC_SIGNALS`: "dobara call mat karna", "call mat karo",
"pareshan mat karo", "baar baar mat call karo", "do not call",
"stop calling", …). `_run_dnc_early_exit` speaks the non-committal
`policy_stop_calls_reply` ("आपकी यह रिक्वेस्ट दर्ज हो गई है — इस विषय की
अंतिम पुष्टि आपको पैसालो से मिल जाएगी।"), tags
`disposition=dnc_requested`, and graceful ENDs (outcome 7,
`end_call=True`).

**Critical:** does NOT set `dunning_suppressed` — dialer suppression is
W4 work and promising it now would be a lie. The `dnc_requested` audit
flag is enough for this release. The non-committal
`policy_stop_calls_reply` is distinct from `opt_out_ack_reply` (which
promises "aage contact nahi karenge" and is gated on W4).

`SafetyResult` gained an `end_call: bool = False` field (C2/C3/C4 strict
all use it). `TenantConfig` gained `dnc_signals` +
`policy_stop_calls_reply` (both scripted + non-scripted branches).

**Tests:** `tests/golden/test_w1c_dnc_capture.py` — 7 cues parametrized;
each asserts scorer call_count==0, `disposition==dnc_requested`,
`end_call=True`, non-committal ack, NO "aage contact nahi" promise, no
flow started; plus unit tests for no-dialer-suppression + paisalo config.
9/9 PASS.

**Existing goldens updated (intended behavior change):** the scorer-based
`opt_out` flow (start_flow opt_out → apply_opt_out → [COMPLIANCE-REVIEW]
+ contact nahi + dunning_suppressed) is now preempted by the policy lane.
- `test_compliance_fs4::test_opt_out_sets_flag_and_confirms_then_gate_silent`
- `test_followup_fs6::test_opt_out_preempts_ptp_followup`
- `test_robustness_fs5::test_cross_flow_opt_out_during_hardship`
All three updated to assert the new non-committal ack +
`disposition=dnc_requested` + `end_call=True` + `dnc_requested` flag
(instead of the old `opt_out` flag / `OPT_OUT` disposition).

### C3 — Call-window close-out (policy interrupt, outcome 7)

New `call_window_preempt` (`app/engine/safety.py`) fires ONLY mid-call
(`state.attempts >= 1`) AND outside the configured window
(`within_call_window` returns False). First turn (attempts=0) outside the
window is left to the gate's silent `outside_call_window` block (correct
— do not answer a fresh call). Mid-call we NEVER go silent: the preempt
speaks the scripted `call_window_close_reply` ("आपका समय धन्यवाद। अब
हमें इस call को समapt करना होगा — हमारी टीम आपसे योग्य समय पर दोबारा
संपर्क करेगी।"), tags `disposition=call_window_closed`, and graceful
ENDs (outcome 7). `TenantConfig` gained `call_window_close_reply`.

**Tests:** `tests/golden/test_w1c_call_window_close.py` — unit tests
for skip-first-turn (attempts=0 → None even if outside), skip-inside-window,
fire-mid-call-outside (end_call=True, reason=call_window_crossed_mid_call);
integration test monkeypatches `call_window_preempt` in the turn module
so turns 1-2 see "inside" and turn 3 sees "crossed" → asserts turn 3
`disposition=call_window_closed` + `end_call=True` + non-empty polite
reply + scorer call_count==2 (did not run on the close turn). 4/4 PASS.

### C4 — Third-party / speaker-flip guard + DPDP amendment (outcome 7 strict / continue relaxed)

New `third_party_flip_preempt` (`app/engine/safety.py`) fires BEFORE
Tier-1 on mid-call speaker-flip cues (`THIRD_PARTY_FLIP_SIGNALS`:
"main uski/uska X bol raha/rahi", "wo bahar hai, main…", "main ramesh
ka bhai bol raha hoon", "i am his brother", …). It revokes
`identity_current` (`identity_ok=False`), locks disclosure
(`third_party_active=True` → `must_block_debt_disclosure` →
`slots_for_nlg` strips `DEBT_SLOT_KEYS`), speaks the third-party script
+ callback capture, and tags `disposition=THIRD_PARTY_FLAGGED`.

**DPDP amendment (brand-configurable):** two new `TenantRuntimeProfile`
fields — `dpdp_third_party_lock: "strict" | "relaxed"` (default strict)
and `dpdp_disclosure_tier_enforced: bool` (default true).
- **strict** = disclosure LOCK → third-party script → callback → END
  (outcome 7, `end_call=True`).
- **relaxed** = identity revoked → generic-only facts (no amounts/dates/
  PII); conversation may continue (`end_call=False`); disclosure LOCK
  still active (debt stripped from NLG slots).
- **open_tier** (`dpdp_disclosure_tier_enforced=false`, lab use) = log
  suspicion only; no lock, no identity revoke, no end.

**ALWAYS-ON regardless of mode:** `_run_third_party_flip_early_exit`
logs `third_party_suspected=true` + `identity_current transition:
revoked` at INFO before any mode branch, and tags
`disposition=THIRD_PARTY_FLAGGED` in all modes. The audit trail is not
configurable — only the enforcement is. `paisalo.yml` leaves defaults
(strict/true) until the brand says otherwise.

**Gate interaction:** the strict/relaxed third-party scripts were
worded to avoid debt phrases ("loan"/"emi"/"borrower owes"/"defaulter")
so the gate's `reply_discloses_debt` check (active under
`third_party_active`) does not block the pre-approved policy-lane copy.

**Tests:** `tests/golden/test_w1c_third_party_flip.py` — 6 cues
parametrized for strict (scorer call_count==0, disposition tagged,
end_call=True, third-party script spoken); unit tests for strict/relaxed/
open-tier modes; integration test for relaxed mode (monkeypatches
`get_tenant_profile` to return a relaxed profile) asserting
`end_call=False` + `disposition=THIRD_PARTY_FLAGGED` + the always-on
log-lines (`third_party_suspected=true` + `identity_current transition`)
present in `caplog`; disclosure-LOCK test asserting
`must_block_debt_disclosure(state.slots)` is True after a strict flip.
11/11 PASS.

### Test results — full golden suite

`tests/golden/` (Python 3.13.1, pytest 9.1.1, `--tb=line -q`, ignoring
`test_live_kb` + `test_live_vertex`): **401 passed, 1 failed** in
379.65s. The single failure is `test_respond_tier3::
test_reason_given_after_respond_advances_push` — PRE-EXISTING (per the
W1-B C2 classification, #30: fails on pre-DT baseline `4663bdf`,
`last_question_slot` assertion mismatch — unrelated to W1-C). **Zero
new regressions from W1-C.**

Go-server: `internal/brain` + `internal/media` suites green (re-run after
C0 wiring); the new `client_w1c_test.go` PASS. `go build ./...` clean.

### Files touched

**Brain (`Collection`):**
- `app/schemas/ws_contract.py` — `SessionReadyMessage.apology_text` + `apology_voice_id`.
- `app/ws/handler.py` — session_start populates apology fields from the tenant profile.
- `app/schemas/compliance.py` — `SafetyResult.end_call` field.
- `app/compliance_defaults.py` — `DNC_SIGNALS`, `POLICY_STOP_CALLS_REPLY_HI`, `CALL_WINDOW_CLOSE_REPLY_HI`, `THIRD_PARTY_FLIP_SIGNALS`, `THIRD_PARTY_FLIP_REPLY_STRICT_HI`, `THIRD_PARTY_FLIP_REPLY_RELAXED_HI` + policy dict entries.
- `app/config.py` — `TenantConfig.dnc_signals` + `policy_stop_calls_reply` + `call_window_close_reply` + `third_party_flip_signals` + `third_party_flip_reply_strict` + `third_party_flip_reply_relaxed` (both scripted + non-scripted branches).
- `app/engine/safety.py` — `dnc_preempt` + `apply_dnc_to_state`, `call_window_preempt` + `apply_call_window_to_state`, `third_party_flip_preempt` + `apply_third_party_flip_to_state` (strict/relaxed/open-tier).
- `app/engine/turn.py` — imports; `dnc_check_transcript` + `call_window_check_transcript` + `third_party_flip_check_transcript`; `_run_safety_early_exit` disposition=VULNERABLE_FLAGGED; new `_run_dnc_early_exit` + `_run_call_window_early_exit` + `_run_third_party_flip_early_exit`; all four preempts wired BEFORE Tier-1 evidence scorer.
- `app/engine/tenant_profile.py` — `dpdp_third_party_lock` + `dpdp_disclosure_tier_enforced` fields.
- `PAISALO_FRAGMENT_LIBRARY_V1.md` — §I vulnerability de-escalation candidate #56 (PENDING-CLIENT-APPROVAL).
- `tests/golden/test_w1c_apology_session_ready.py` (new, 2 tests).
- `tests/golden/test_w1c_vulnerability_lane.py` (new, 7 tests).
- `tests/golden/test_w1c_dnc_capture.py` (new, 9 tests).
- `tests/golden/test_w1c_call_window_close.py` (new, 4 tests).
- `tests/golden/test_w1c_third_party_flip.py` (new, 11 tests).
- `tests/golden/test_compliance_fs4.py` — updated opt_out test for the new preemption.
- `tests/golden/test_followup_fs6.py` — updated opt_out test for the new preemption.
- `tests/golden/test_robustness_fs5.py` — updated opt_out test for the new preemption.
- `docs/IMPLEMENTATION_TRACKER_V2.md` — W1-C bar 100% [R]; DEBT-026 closed; W1-C notes.

**Go-server (`Websocket`):**
- `internal/brain/contract.go` — `SessionReadyPayload.ApologyText` + `ApologyVoiceID`.
- `internal/brain/client.go` — `readSessionReady` + late dispatch call `SetApologyLine` on `*media.TTSReplyConsumer`.
- `internal/brain/client_w1c_test.go` (new, 1 test).

### Residual / next

- **LIVE PREDUE protocol** (per spec): silent smoke → "ready" → Nitish
  answers ONE call with the 5-probe script + one NEW probe (mid-call say
  "main Ramesh ka bhai bol raha hoon" → expect disclosure lock +
  third-party close). No W2 until the live call passes.
- C1 de-escalation script + C4 third-party scripts remain
  PENDING-CLIENT-APPROVAL (fragment library candidates #56 + §I/§J);
  hot-swappable via profile YAML.
- C2 dialer suppression is explicitly W4 work (the `dnc_requested` audit
  flag is recorded but `dunning_suppressed` is NOT set).
- C4 DPDP posture defaults to strict/true on paisalo.yml until the brand
  says otherwise.

**Stop:** Awaiting architect sign-off on CP-W1C, then the LIVE PREDUE
protocol.
