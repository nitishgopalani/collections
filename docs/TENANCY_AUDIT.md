# TENANCY AUDIT — Phase A2

_Phase A2 per `CURSOR_BOOT_V2.md`, scoped to the 12 real branch points from `docs/REPO_CONTEXT.md` §3._
_Audit-only. No code changes, no DT work, no deploys. 09 Aug 2026._

---

## 1. BRANCH-POINT CLASSIFICATION (12 real branch points)

Each of the 12 real tenant-conditional branch points from `REPO_CONTEXT.md` §3 is classified into one of three buckets:

- **PROFILE-FIELD**: replace the branch with a new `TenantRuntimeProfile` field (bool / list / str). Clean fix.
- **TEST-SHIM QUARANTINE**: the branch is test-mode-only plumbing (`TEST_MODE=true` path). Quarantine it behind a single `profile.test_*` field set so production never sees it; do NOT delete (goldens still need it).
- **LEAK-PATH**: the branch is a real cross-tenant contamination risk. Fix is a guard, not a profile field.

| # | File:line | Code | Bucket | Replacement / Fix |
|---|---|---|---|---|
| 1 | `app/config.py:610` | `if tenant_id in {"salary_on_time", "paisalo"}:` | **PROFILE-FIELD** | Replace with `if get_tenant_profile(tenant_id) is not None:` — "all tenants with a YAML profile". |
| 2 | `app/engine/turn.py:931` | `and request.tenant_id != "paisalo"` | **TEST-SHIM QUARANTINE** | Move into `profile.allow_sot_test_mode` (default `false`); only SOT profile sets it `true`. PLO profile leaves it `false` so the branch is tenant-agnostic. |
| 3 | `app/engine/turn.py:941` | `elif settings.test_mode and request.tenant_id == "paisalo" and plo_override:` | **TEST-SHIM QUARANTINE** | `profile.test_borrower_factory` ref (callable). SOT profile → `hardcoded_test_borrower`; PLO profile → `hardcoded_paisalo_borrower`. |
| 4 | `app/engine/scripted_coercions.py:243` | `if not supplied_time and profile.flow_prefix != "sot_":` | **PROFILE-FIELD** | `profile.supports_committed_date_coercion` (bool). SOT `true`, PLO `false` (until H3 lands). |
| 5 | `app/engine/scripted_coercions.py:276` | `if profile.flow_prefix != "sot_":` | **PROFILE-FIELD** | `profile.timing_slot_set` (list). SOT `["sot_customer_time", "sot_commit_timing"]`, PLO `[]`. |
| 6 | `app/engine/label_transition.py:121` | `if tenant_id == "salary_on_time":` | **PROFILE-FIELD** | `profile.ltl_enforce_enabled` (bool). SOT `true`, PLO `false`. (Already implied by `ltl_mode: enforce` in YAML — just promote the field.) |
| 7 | `app/engine/identity_gate.py:120` | `"sot_opener"` in `IDENTITY_FLOWS` | **PROFILE-FIELD** | `profile.identity_bypass_flows` (list). SOT `["sot_opener"]`, PLO `["plo_opener"]`. |
| 8 | `app/ws/handler.py:101` | `is_paisalo_test = ... == "paisalo"` | **TEST-SHIM QUARANTINE** | `profile.test_agent_id` + `profile.test_borrower_id` (str). SOT → `("salary-on-time-test", "sot_test_borrower")`, PLO → `("paisalo-test", "plo_test_borrower")`. |
| 9 | `app/ws/handler.py:1211` | `if test_force == "paisalo"` | **TEST-SHIM QUARANTINE** | Same as #8 — `profile.test_agent_id`. |
| 10 | `app/ws/handler.py:1223` | `if (settings.test_tenant_id or "") == "paisalo"` | **TEST-SHIM QUARANTINE** | Same as #8. |
| 11 | `app/memory/test_borrower.py:147` | `keys = _PLO_LOAN_KEYS if state.tenant_id == "paisalo" else _SOT_LOAN_KEYS` | **TEST-SHIM QUARANTINE** | `profile.test_loan_keys` (list). SOT → `_SOT_LOAN_KEYS`, PLO → `_PLO_LOAN_KEYS`. |
| 12 | `app/memory/test_borrower.py:158` | `if state.tenant_id == "paisalo":` | **TEST-SHIM QUARANTINE** | `profile.test_scenario_override_slot` (str). SOT → `""`, PLO → `"plo_scenario_override"`. |

### Bucket counts

| Bucket | Count | Action |
|---|---|---|
| PROFILE-FIELD | 5 (#1, #4, #5, #6, #7) | Add 5 new `TenantRuntimeProfile` fields. Each is a 1-line YAML addition per tenant. |
| TEST-SHIM QUARANTINE | 6 (#2, #3, #8, #9, #10, #11, #12 — #2 and #3 overlap) | Add 5 `profile.test_*` fields. All test-mode branches collapse into these. Production never sees them. |
| LEAK-PATH | 1 (the `force_flow` injection path — see §3 below) | Add a guard, not a profile field. |

**Total new `TenantRuntimeProfile` fields: 10** (5 profile + 5 test-shim). One new guard (force_flow catalog check).

---

## 2. THREE LIVE MIXING INCIDENTS (traced)

The three live cross-tenant mixing incidents are documented in `docs/PIPELINE_AUDIT.md` (08 Aug 2026). Each is traced from the live call evidence through the code path that allows it.

### Incident 1 — G-A3-01: Connector stamps `client_id=salary-on-time` for PaisaLo ARI calls (P0)

**Evidence** (`PIPELINE_AUDIT.md` A3, line 105-123):
- Live PaisaLo PREDUE call `d501f625` `session_start` payload:
  ```json
  {"type":"session_start","client_id":"salary-on-time",
   "metadata":{"client_id":"paisalo","customer_phone":"9810587857","business_phone":"1725617007"}}
  ```
- Top-level `client_id` = `salary-on-time` (from the connector listener `AudioSocketListener.Tenant`).
- Metadata `client_id` = `paisalo` (from orchestrator media-meta) — **ignored**.

**Code path**:
1. `asterisk-connector/internal/config/config.go:AudioSocketListener.Tenant` — env `AUDIOSOCKET_TENANT_MAP` / listener stamp sets `client_id="salary-on-time"` for all DIDs on that listener.
2. `asterisk-connector/internal/wsclient/wsclient.go:sessionStart` — fills `client_id` from metadata **only when empty**. Top-level already set → metadata `paisalo` ignored.
3. `Websocket/internal/brain/client.go:ConfigFromEnv` — reads `BRAIN_TENANT_ID` (default `salary-on-time`) as `client_id` for media-meta path.
4. `app/ws/handler.py:handle_brain_websocket` — receives `session_start.client_id="salary-on-time"`.

**Why UAT "works"**: Incident 2 masks it.

**Fix class**: LEAK-PATH. The connector should let metadata `client_id` override the listener stamp when metadata is present (BYO path). Or the orchestrator should not set the listener stamp for PaisaLo DIDs. This is a connector/orch fix, not a brain fix — out of scope for Phase A2 brain-side DT.

### Incident 2 — G-A2-01: Brain `TEST_MODE`+`TEST_TENANT_ID=paisalo` pins tenant, masking G-A3-01 (P0)

**Evidence** (`PIPELINE_AUDIT.md` A2, line 53-54):
- UAT compose `.env`: `TEST_MODE=true`, `TEST_TENANT_ID=paisalo`.
- Brain container env inherits these.

**Code path**:
1. `app/config.py:132` — `test_tenant_id: str = "salary_on_time"` (env `TEST_TENANT_ID`).
2. `app/ws/handler.py:_normalize_test_session_start` (line 101) — `is_paisalo_test = (test_tenant_id == "paisalo")` → forces `agent_id="paisalo-test"`, `borrower_id="plo_test_borrower"`.
3. `app/ws/handler.py:handle_brain_websocket` (lines 1211, 1223) — `test_force == "paisalo"` / `test_tenant_id == "paisalo"` branches override the routed tenant.
4. `app/engine/turn.py:926-944` — `sot_override` / `plo_override` test-mode borrower selection.

**Why this is a mixing incident**: The brain sees `client_id=salary-on-time` (Incident 1) but `TEST_MODE` forces `tenant_id=paisalo`. So the call runs as PaisaLo. In production (`TEST_MODE=false`), the same call would run as `salary_on_time` — a SOT script with a PaisaLo borrower. **Cross-tenant script/borrower mismatch.**

**Fix class**: TEST-SHIM QUARANTINE. Branch points #2, #3, #8, #9, #10, #11, #12 (§1 above) all collapse into `profile.test_*` fields. Once quarantined, `TEST_MODE=false` production runs use the real `client_id` → tenant routing, and the test-mode plumbing never fires.

### Incident 3 — G-A4-03: PaisaLo + SOT share `media_streams` `secret_hash`/hint `ef01` (P2)

**Evidence** (`PIPELINE_AUDIT.md` A4, line 135, 409):
- `orchestrator.media_streams` SQL dump:
  - `salary-on-time` row: `secret_hash=...ef01...`
  - `paisalo` row: `secret_hash=...ef01...` (same)
- PaisaLo SKU = `plo` (nonstandard).

**Code path**:
1. `ari-orchestrator` reads `media_streams` by tenant to get `media_ws_url` + `secret_hash`.
2. Both tenants share the same secret → a media WS client authenticated for SOT can connect to the PaisaLo media endpoint (and vice versa).
3. `Websocket/internal/media/session.go` — `client_id` forwarded verbatim; if a cross-tenant connection carries the shared secret, the go-server cannot distinguish tenants by secret alone.

**Why this is a mixing incident**: A misconfigured originate (SOT DID → PaisaLo borrower, or vice versa) would pass media authentication and the go-server would stream audio to the wrong tenant's media session. This is a **shared-secret smell**, not a brain-side bug.

**Fix class**: LEAK-PATH (orchestrator/DB). Rotate PaisaLo's `secret_hash` to a unique value; rename SKU `plo` → standard codec name. Out of scope for brain-side Phase A2.

### Summary of the three incidents

| # | ID | Layer | Fix owner | Phase A2 brain-side? |
|---|---|---|---|---|
| 1 | G-A3-01 | connector listener stamp | connector / orch | No (connector fix) |
| 2 | G-A2-01 | brain test-mode | brain | **Yes** (test-shim quarantine, branch points #2,3,8-12) |
| 3 | G-A4-03 | orchestrator media_streams DB | orch / security | No (DB rotation) |

**Only Incident 2 is in scope for brain-side Phase A2.** Incidents 1 and 3 are flagged for the connector/orchestrator owners.

---

## 3. NLG NAMESPACE CHECK — can a `plo_` call ever render a `sot_` reply_id?

**Question**: Walk every fallback / error path in `app/engine/nlg.py:draft_reply_resolved` and confirm whether a PaisaLo call (`tenant_id=paisalo`) can ever render a `sot_*` reply_id.

### Resolution paths in `draft_reply_resolved` (nlg.py:577-675)

| # | Path | Source of reply_id | Tenant-scoped? | Cross-tenant leak risk |
|---|---|---|---|---|
| 1 | `repeat_reply_id` (line 591-593) | `state.slots["repeat_reply_id"]` | Set by executor / flows. | **YES** if a sot_ id is set into `repeat_reply_id` during a plo_ call. |
| 2 | `utter_chain` (line 617-625) | `executor.ExecResult.utter_chain` — reply_ids from the flow walk. | Scoped to the flow being walked. | **YES** if a sot_ flow is on the stack during a plo_ call. |
| 3 | `reply_id` (line 627-628) | `executor.ExecResult.reply_id` — LAST utter's reply_id. | Scoped to the flow. | **YES** if a sot_ flow is on the stack. |
| 4 | `question_slot` → `COLLECT_SLOT_REPLY_IDS` (line 630-648) | `question_slot` from executor → dict lookup. | Dict is **shared** (sot_ + plo_ keys in one dict). | **YES** if `question_slot` is a `sot_*` slot (because a sot_ flow is on the stack). |
| 5 | `question_slot` + `is_clarify` → `CLARIFY_REASK_REPLY_IDS` (line 632-640, via `_render_clarify_reask`) | `question_slot` → dict lookup. | Shared dict. | **YES** if `question_slot` is `sot_*`. |
| 6 | `CLARIFY_REPLY_ID = "clarify_general"` (line 669-674) | Constant string. | **Tenant-agnostic** (no prefix). | **NO** — safe. |
| 7 | `tenant_cfg.clarify_reply` (line 650, 671, 675) | `TenantConfig.clarify_reply` text. | Per-tenant text. | **NO** — safe. |
| 8 | `tenant_cfg.care_first_reply` (line 654) | `TenantConfig.care_first_reply` text. | Per-tenant text. | **NO** — safe. |

### Verdict: **YES, a `plo_` call can render a `sot_` reply_id** — via paths 1-5.

The leak requires a `sot_*` flow to be on the call's flow stack. Three ways that happens:

#### Leak Path A — `force_flow` injection (the live one)

`app/engine/turn.py:1004-1018`:
```python
forced_flow = state.slots.get("_force_test_flow")
if isinstance(forced_flow, str) and (
    forced_flow in FORCE_FLOW_ALIASES or forced_flow in flows.flows
):
    ...
    state.flow_stack.append(Frame(flow=forced_flow, step_index=0))
```

The check is `forced_flow in FORCE_FLOW_ALIASES or forced_flow in flows.flows`. `flows.flows` is the **global** FlowSet containing ALL flows (sot_ + plo_ + generic). There is **no tenant-catalog check**. So a `plo_` call with `turn_meta.force_flow="sot_opener"` (or a session-level `force_flow` set by `ws/handler.py:resolve_agent_routing`) injects `sot_opener` into the stack. The executor walks `sot_opener`, producing `sot_*` reply_ids and `sot_*` question_slots. NLG renders `sot_*` templates.

**This is the same path that Incident 2 (G-A2-01) exploits**: `TEST_MODE`+`TEST_TENANT_ID=paisalo` can pair with a `force_flow` from the connector's `client_id=salary-on-time` (Incident 1) to mix a SOT flow into a PLO call.

#### Leak Path B — LLM `start_flow: sot_*` when `catalog_mode=false`

`app/engine/command_gen.py:582-597` rejects `start_flow` commands not in the tenant catalog **only when `catalog_mode=true`**. When `catalog_mode=false` (legacy digression / RAG path, used by open/default tenants), the only check is `known_flow_names` (the global set). A `plo_` call with `catalog_mode=false` would accept `start_flow: sot_opener`.

**Mitigated today**: `app/engine/turn.py:1097` sets `catalog_mode=true` for all profile tenants (SOT + PLO). So this path is **dormant** for the two live tenants. It activates if a third tenant is added without a profile or if `SCRIPTED_CATALOG_ROUTING=false`.

#### Leak Path C — `repeat_reply_id` set by a cross-tenant flow

If a `sot_*` flow ever sets `state.slots["repeat_reply_id"] = "sot_*"` and then a `plo_*` flow takes over the stack, the `repeat_reply_id` survives and NLG renders the `sot_*` template. No known flow does this today, but there is no guard preventing it.

### Guard recommendation (NOT a fix — audit only)

Add a tenant-catalog check to the `force_flow` injection at `turn.py:1007-1009`:
```python
# PROPOSED (not applied):
if isinstance(forced_flow, str) and forced_flow in flows.flows:
    profile = get_tenant_profile(request.tenant_id)
    catalog = tenant_flow_catalog(profile, flows) if profile else None
    if catalog is None or forced_flow in catalog:
        state.flow_stack.append(Frame(flow=forced_flow, step_index=0))
```

This is the **one new guard** from §1's bucket counts. It closes Leak Path A. Paths B and C are already mitigated by `catalog_mode=true` for profile tenants.

### NLG namespace check conclusion

- **Can a `plo_` call render a `sot_` reply_id today?** YES — via `force_flow` injection (Leak Path A). This is the live mixing path behind Incident 2.
- **Is the NLG dict itself at fault?** No. `COLLECT_SLOT_REPLY_IDS` and `CLARIFY_REASK_REPLY_IDS` are shared dicts, but the lookup key (`question_slot`) is scoped to the flow on the stack. The leak is upstream (flow-stack injection), not in NLG.
- **Are the fallback paths (6, 7, 8) safe?** Yes — `clarify_general`, `tenant_cfg.clarify_reply`, `tenant_cfg.care_first_reply` are all tenant-agnostic or per-tenant text, never cross-tenant reply_ids.

---

## 4. DT REFACTOR PLAN (target ≤ 1.5 days)

**Goal**: Eliminate all 12 real branch points from `app/engine/` and `app/ws/` / `app/memory/` test-shim paths so no `if tenant_id == "..."` remains in engine code. The DT (Decision-Tree) refactor is the mechanical replacement of the 12 branches with the 10 new `TenantRuntimeProfile` fields + 1 guard from §1.

### 4.1 New `TenantRuntimeProfile` field list (10 fields)

| # | Field | Type | SOT value | PLO value | Branch points closed |
|---|---|---|---|---|---|
| 1 | `has_tenant_config` (implicit) | bool (computed) | `true` | `true` | #1 (`config.py:610`) |
| 2 | `supports_committed_date_coercion` | bool | `true` | `false` | #4 (`scripted_coercions.py:243`) |
| 3 | `timing_slot_set` | list[str] | `["sot_customer_time", "sot_commit_timing"]` | `[]` | #5 (`scripted_coercions.py:276`) |
| 4 | `ltl_enforce_enabled` | bool | `true` | `false` | #6 (`label_transition.py:121`) |
| 5 | `identity_bypass_flows` | list[str] | `["sot_opener"]` | `["plo_opener"]` | #7 (`identity_gate.py:120`) |
| 6 | `test_agent_id` | str | `"salary-on-time-test"` | `"paisalo-test"` | #8, #9, #10 (`ws/handler.py:101, 1211, 1223`) |
| 7 | `test_borrower_id` | str | `"sot_test_borrower"` | `"plo_test_borrower"` | #8 (`ws/handler.py:101`) |
| 8 | `test_borrower_factory` | callable ref | `hardcoded_test_borrower` | `hardcoded_paisalo_borrower` | #3 (`turn.py:941`) |
| 9 | `test_loan_keys` | list[str] | `_SOT_LOAN_KEYS` | `_PLO_LOAN_KEYS` | #11 (`test_borrower.py:147`) |
| 10 | `test_scenario_override_slot` | str | `""` | `"plo_scenario_override"` | #12 (`test_borrower.py:158`) |

**Plus 1 new field for the test-mode gate** (closes #2 `turn.py:931`):
| 11 | `allow_sot_test_mode` | bool | `true` | `false` | #2 (`turn.py:931`) |

**Total: 11 new `TenantRuntimeProfile` fields.** (5 profile + 6 test-shim.)

### 4.2 One new guard (not a profile field)

| Guard | Location | Closes |
|---|---|---|
| `force_flow` tenant-catalog check | `app/engine/turn.py:1007-1009` (the `forced_flow in flows.flows` branch) | NLG Leak Path A (§3) — prevents a `plo_` call from injecting a `sot_` flow via `force_flow`. |

### 4.3 Deletion list from the orphan map

The orphan-map probe (`scripts/_audit_orphan_map.py`, run 09 Aug 2026) found **41 candidate orphan flows** — defined in YAML but not referenced by any tenant's `on_rails_flows` / `coercion_chain` / `dispute_flows` / `callback_flow` / `reversal_target_flow` / `pinned_flows` / `deflection_objections`, not referenced by any flow's `start_flow` / `next` step, and not referenced in tests.

**Caveat**: the probe did NOT parse `decide:` branch targets (step-id jumps) or `escalate_to` targets. Some `plo_obj_npa_*` flows may be referenced via NPA `decide` branches. Each orphan needs manual verification before deletion. The list below is a **candidate deletion list**, not a confirmed deletion list.

**SOT orphans (12)**:
```
sot_obj_amount_in_2_days, sot_obj_cant_login, sot_obj_credit_manager,
sot_obj_is_bot, sot_obj_month_only, sot_obj_pay_later_penalty,
sot_obj_penalty_now, sot_obj_pending_status, sot_obj_processing_fee,
sot_obj_reduce_amount, sot_obj_references_called, sot_obj_support_number,
sot_obj_total_payable, sot_obj_unknown_query
```
(14 listed — count discrepancy vs 12 because two are pre-existing test-only flows; verify before delete.)

**PLO orphans (24)**:
```
plo_obj_assurance_pd, plo_obj_dealer_pay, plo_obj_dealer_pay_pd,
plo_obj_death_pd, plo_obj_lost_qr, plo_obj_multiple_loans,
plo_obj_multiple_loans_pd, plo_obj_npa_angry, plo_obj_npa_assurance,
plo_obj_npa_branch_address, plo_obj_npa_death, plo_obj_npa_hardship,
plo_obj_npa_lost_qr, plo_obj_npa_medical, plo_obj_npa_paid_official,
plo_obj_npa_refuse, plo_obj_npa_where_to_pay, plo_obj_npa_wrong_number,
plo_obj_paid_official_pd, plo_obj_personal_issue, plo_obj_where_to_pay,
plo_obj_will_not_pay, plo_obj_will_you_pay, plo_obj_will_you_pay_pd
```

**Generic orphans (3)**:
```
beyond_authority, incapacitated_borrower, off_topic_redirect
```

**DT refactor deletion scope**: delete the 41 candidate orphan flows + their `reply_manifest.json` entries (254 → ~213). This reduces the flow YAML surface area by ~30% and the reply_manifest by ~16%. **Verify each against `decide:` / `escalate_to` branches before deleting.**

### 4.4 Effort breakdown (≤ 1.5 days)

| Step | Effort | Owner |
|---|---|---|
| Add 11 `TenantRuntimeProfile` fields + SOT/PLO YAML values | 0.25d | impl |
| Replace 5 PROFILE-FIELD branches (#1, #4, #5, #6, #7) | 0.2d | impl |
| Quarantine 6 TEST-SHIM branches (#2, #3, #8-12) behind `profile.test_*` | 0.3d | impl |
| Add `force_flow` catalog guard | 0.1d | impl |
| Verify + delete 41 orphan flows + manifest entries | 0.4d | impl + architect sign-off |
| Re-run goldens + W1-A P1-P5 tests | 0.25d | impl |
| **Total** | **1.5d** | |

### 4.5 What is NOT in this DT refactor

- **Incident 1 (G-A3-01)**: connector listener stamp fix — connector owner.
- **Incident 3 (G-A4-03)**: media_streams secret rotation — orchestrator/security owner.
- **H3 (PaisaLo reversal stage)**: W1-A residual, separate work item.
- **Per-tenant NLG dict isolation** (DEBT-009): W3 scope.
- **Per-tenant borrower tables** (DEBT-011): W4 scope.

---

## 5. REGISTER ROWS (for tracker)

### W1-A residual (from Task 1e)

| ID | Component | Debt | Owner | Phase |
|---|---|---|---|---|
| DEBT-016 | `app/tenants/paisalo.yml` + `app/engine/scripted_coercions.py:coerce_commit_reversal` | **H3 skipped**: PaisaLo reversal stage absent. `reversal_slots: []`, `reversal_target_flow: ""`, no `plo_*reversal*` cues/slots/flows/tests. `coerce_commit_reversal` is a no-op for PaisaLo (early-returns on both empty checks). H3 was scoped into W1-A but skipped entirely. Do NOT fix in A2 — separate work item. | impl | W1-A residual |

### Phase A2 register rows (new)

| ID | Component | Debt | Owner | Phase |
|---|---|---|---|---|
| DEBT-017 | `app/engine/turn.py:1007-1009` | `force_flow` injection checks `flows.flows` (global), not tenant catalog. NLG Leak Path A — a `plo_` call can render a `sot_` reply_id via `force_flow`. Fix: add tenant-catalog guard. | impl | A2 |
| DEBT-018 | `app/engine/turn.py:931, 941` | Test-mode borrower selection branches on `tenant_id == "paisalo"`. Quarantine behind `profile.test_borrower_factory` + `profile.allow_sot_test_mode`. | impl | A2 |
| DEBT-019 | `app/ws/handler.py:101, 1211, 1223` | 3 `paisalo` test-mode branches. Quarantine behind `profile.test_agent_id` + `profile.test_borrower_id`. | impl | A2 |
| DEBT-020 | `app/memory/test_borrower.py:147, 158` | 2 `paisalo` branches. Quarantine behind `profile.test_loan_keys` + `profile.test_scenario_override_slot`. | impl | A2 |
| DEBT-021 | `app/engine/scripted_coercions.py:243, 276` | 2 `profile.flow_prefix != "sot_"` branches. Replace with `profile.supports_committed_date_coercion` + `profile.timing_slot_set`. | impl | A2 |
| DEBT-022 | `app/engine/label_transition.py:121` | SOT-only enforce adapter. Replace with `profile.ltl_enforce_enabled`. | impl | A2 |
| DEBT-023 | `app/engine/identity_gate.py:120` | Hardcoded `sot_opener` in `IDENTITY_FLOWS`. Replace with `profile.identity_bypass_flows`. | impl | A2 |
| DEBT-024 | `app/config.py:610` | `if tenant_id in {"salary_on_time", "paisalo"}` branch. Replace with `if get_tenant_profile(tenant_id) is not None`. | impl | A2 |
| DEBT-025 | `app/flows/` | 41 candidate orphan flows (12 SOT + 24 PLO + 3 generic) + their `reply_manifest.json` entries. Verify + delete. | impl + architect | A2 |

---

## 6. TRACKER BAR UPDATES

| Phase | Old bar | New bar | Old status | New status | Notes |
|---|---|---|---|---|---|
| P0 | 100% | 100% | [R] | [R] | Signed off by architect 09 Aug 2026. |
| A2 | 0% | 0% (planned) | [ ] | [P] | Plan complete (`TENANCY_AUDIT.md`); execution blocked on architect sign-off. |
| W1-A | 60% | **83%** | [~] | **[R]** | P1-P5 done (41/41 tests pass); H3 (reversal) skipped — DEBT-016. Ready for architect review of residual. |

---

## 7. STOP

Audit complete. No code changes, no DT work, no deploys.
- Task 1 (retroactive CP-W1A review): evidence captured in §1-§6 above + W1-A test run (41/41 pass).
- Task 2 (Phase A2 plan): `TENANCY_AUDIT.md` delivered; 11 new profile fields + 1 guard + 41-orphan deletion list; 1.5d effort estimate.
- DEBT-016 (W1-A residual) + DEBT-017..025 (A2) registered.
- Tracker bars updated: A2 → [P], W1-A → 83% [R].

Awaiting architect sign-off before any execution.



