# REPO CONTEXT — Fonada Voice Collections Stack

_Audit/document anchor for the v1.3-final build-out. Produced at PHASE 0 (CURSOR_BOOT_V2)._
_Scanned at brain HEAD `958135d` (feature/tier23-engine-upgrade), go-server `4e52063` (release/uat-voice-stack), connector `0b7a252` (feature/async-ws-audio-writes), orchestrator `2408ce2`. 09 Aug 2026._

## SURPRISES FOUND AT SCAN (read first)

1. **Brain HEAD drift.** CURSOR_BOOT_V2.md / MASTER_SPRINT_PLAN.md state brain HEAD = `810647d`. Actual HEAD at scan = `958135d` ("PLO-OOF: PaisaLo out-of-flow completion (P1-P5)"). The PLO-OOF commit landed after the boot doc was written, so W1-A P1-P5 work (cue packs, re-ask ladder, grounding forensic, B-side quick wins, committed_date) is already on the branch. The boot doc's "W1 GREENLIT, NOT STARTED" line is stale. Confirm with the architect before re-scoping W1-A.

2. **Chat-only docs are 0-byte on disk.** `CURSOR_BOOT_V2.md`, `IMPLEMENTATION_TRACKER_V2.md`, `MASTER_SPRINT_PLAN.md`, `PAISALO_FRAGMENT_LIBRARY_V1.md`, `VOICE_STACK_ARCHITECTURE.html`, `W2_SPRINT_SPEC.md` at the `Collection/` root are 0-byte placeholder files (untracked in git). Their content lives only in the IDE editor buffer (unsaved). This scan used the IDE-buffer content. They are NOT committed by CP0 because the on-disk files are empty; saving them to disk is the user's action.

3. **CURSOR_HANDOVER.md is missing.** The boot spec lists `CURSOR_HANDOVER.md` as one of the five chat-only docs to commit. No such file exists anywhere under `c:\Users\nitis\source\repos\Main` (recursive search returned 0 hits). It was never created. The "8 live-bug classes" referenced by the boot doc are therefore not recoverable from the repo; only the boot doc's own standing-rules line carries that history forward.

4. **PIPELINE_AUDIT.md already in docs/.** `docs/PIPELINE_AUDIT.md` (28293 B, 08 Aug 2026) already exists and is untracked (`??`). No action needed beyond `git add`.

5. **Uncommitted work in brain working tree.** `git status` shows 8 modified golden/script transcript files under `scripts/` (test-order pollution suspects) and ~180 untracked scripts/scratch files. None are touched by this audit.

---

## 1. MODULE MAP

### Brain — `Collection/app/` top-level

| File | One-liner |
|---|---|
| `main.py` | FastAPI app: `/healthz`, POST `/turn` -> `handle_turn`, WS `/ws/brain` -> `handle_brain_websocket`, GET `/v1/conference/{id}/transcript`. Lifespan wires llm/kb/tools/memory/flows/overrides. |
| `config.py` | `Settings` (env-driven), `TenantConfig`, `tenant_config(tenant_id)` resolver, `_TENANT_ROUTING_DEFAULTS` registry, booking-confirm/conference prompt personas. 37 KB. |
| `compliance_defaults.py` | Default compliance policy (prohibited/pressure/vulnerability/distress/critical-inbound phrases, safe-fallback/care-first/clarify/silent replies, collect-slot prompts). |
| `startup_validation.py` | `validate_settings_or_exit()` fail-fast. |
| `telemetry.py` | `turn_trace`/`span`/`annotate_turn_span` turn-span helpers. |
| `exceptions.py` | `StaleStateError` (HTTP 409). |
| `logging_config.py` | `configure_logging()`. |

### Brain — `app/engine/` (turn pipeline)

| File | One-liner |
|---|---|
| `turn.py` | **70 KB single-turn orchestrator.** `handle_turn()`: load state -> hydrate -> safety_preempt -> retrieval/catalog -> command_gen -> coercion chain -> slot validation -> LTL -> tracker.apply -> priority reorder -> decision overlay -> executor -> NLG -> gate -> persist. Plus warm-transfer driver, WhatsApp bg sender, closed-call early-exit. |
| `executor.py` | `run_async(state, flows, action_runner)` flow-stack walker (collect/action/decide/utter/end). `ExecResult{reply_id, utter_chain, question_slot, end_call, disposition}`. Step cap 256. |
| `actions.py` | **66 KB action registry.** `make_async_action_runner(tools)`. Houses `select_sot_scenario`, `select_plo_scenario`, `plo_chain_*`, `sot_*`/`plo_*` resetters, `classify_sot_relation`, `activate_sot_restricted_mode`, plus generic actions (set_slot, start_flow, hangup_call, transfer_call, send_whatsapp_message, ...). |
| `command_gen.py` | LLM command generator (Groq/Vertex). Emits `Command[]` + rejections + raw. `catalog_mode` skips KB scores; `respond_enabled` gates Tier-3. |
| `scripted_coercions.py` | Profile-driven Tier-1 coercion chain: `dispute -> callback -> willing -> refusal -> identity -> reversal -> confirm -> link -> committed_date -> reason_catchall`. `INABILITY_RE` shared. `run_coercion_chain()` entry. |
| `nlg.py` | Templated NLG. `COLLECT_SLOT_REPLY_IDS`, `CLARIFY_REASK_REPLY_IDS`, `CLARIFY_MIN_ROTATION_SLOTS` (all sot_/plo_-keyed). `draft_reply_resolved()`, `render_resolved()`, `render_short_reask()`, `spoken_amount_hindi()`, `spoken_date_hindi()`, `spoken_days_hindi()`. |
| `gate.py` | 4-level compliance gate `gate()`: critical_inbound -> vulnerable -> opt_out -> third_party/pre-identity debt -> dunning_suppressed -> call_window -> attempt_cap -> dispute_hold -> prohibited. Returns `GateResult`. |
| `identity_gate.py` | `apply_identity_entry_gate()`, `defer_collection_flows()`, `must_block_debt_disclosure()`, `slots_for_nlg()`, `reply_discloses_debt()`. `COLLECTION_FLOWS`, `IDENTITY_FLOWS`, `DEBT_SLOT_KEYS`, `DEBT_DISCLOSURE_PHRASES`. |
| `tenant_profile.py` | `TenantRuntimeProfile` pydantic model + `get_tenant_profile(id)` LRU-cached YAML loader from `app/tenants/<id>.yml`. |
| `catalog.py` | `tenant_flow_catalog(profile, flows)` full on-rails catalog for Tier-2; `filter_deflection_objections()`. |
| `retrieval.py` | `retrieve_flow_candidates(kb, transcript, tenant_id)` KB RAG path (legacy/digression). |
| `priority.py` | `reorder(state, flows)` frame priority reorder. |
| `robustness.py` | `track_frustration()`, `track_slot_reask()`, `mark_repair_escalation()`, `record_agent_fault()`, `record_outbound_context()`. |
| `respond_guard.py` | `ground_respond_text()` Tier-3 grounding + 220-char cap. |
| `safety.py` | `safety_preempt()`, `apply_safety_to_state()`. |
| `compliance_rules.py` | `evaluate_pressure_with_allowlist()`, `matches_any()`, `within_call_window()`, `flags()`. |
| `compliance_handoff.py` | `dunning_suppressed()`, `reply_discloses_debt_or_arrears()`, `sync_compliance_notes_on_persist()`. |
| `dispute_breadth.py` | `sync_dispute_on_persist()`. |
| `followup.py` | `hydrate_followup_from_borrower()`, `sync_followup_on_persist()`. |
| `hardship.py` | `sync_hardships_on_persist()`. |
| `label_transition.py` | **25 KB Label Transition Layer (LTL).** `run_label_transition()`. Shadow/enforce. SOT-only enforce adapter. `SOT_LABEL_TO_FLOW`/`SOT_FLOW_TO_LABEL` maps. |
| `latency.py` | `StageTimer`, `TurnLatencyProfile`. |
| `refusal_negotiation.py` | `sync_refusal_negotiation_on_persist()`. |
| `slot_validation.py` | `validate_commands()` drop set_slots that overwrite hydrated facts. |
| `stream_sentences.py` | Sentence-streaming helper for prompt-mode TTS. |
| `tracker.py` | `apply(state, events)`, `new_conversation_state()`, `hydrate_from_borrower()`. |
| `turn_decision_log.py` | `log_turn_decision()` per-turn decision dump. |
| `turn_timing.py` | `PromptTurnTiming`, stage constants. |
| `consult_binding.py` | Consult-call binding (booking-confirm property leg). |
| `consult_voicemail.py` | Voicemail/carrier-phrase detection for consult dial. |
| `prompt_agent.py` | **45 KB prompt-mode agent** (booking-confirm/conference). `handle_prompt_turn()`, `handle_prompt_turn_streaming()`, consult/conference markers, hold pause/resume. |
| `pipeline.py` | Small `transcript_to_commands()` legacy Sprint-4 path (not the live turn loop). |

### Brain — `app/engines_p2/` (Phase-2 shadow engines)

| File | One-liner |
|---|---|
| `decision_overlay.py` | `apply_decision_overlay(state, flows)` risk/trust/emotion overlay. |
| `emotion.py` | `classify_emotion_from_turn()`, `apply_emotion_to_state()`, `sync_emotion_on_persist()`. |
| `persona.py` | `apply_persona_to_state()`, `sync_persona_on_persist()`. |
| `recovery_prob.py` | `apply_recovery_to_state()`, `sync_recovery_on_persist()`. |
| `risk.py` | `apply_risk_to_state()`, `sync_risk_on_persist()`. |
| `trust.py` | `apply_trust_to_state()`, `sync_trust_on_persist()`. |

### Brain — `app/flows/` (flow YAML + loader)

| File / Dir | One-liner |
|---|---|
| `loader.py` | `get_flow_set()` loads all flow YAML + `reply_manifest.json`, caches. |
| `manifest.py` | `MANIFEST_VERSION`, `load_reply_manifest()`. |
| `validator.py` | Flow YAML schema validation. |
| `overrides.py` | `merge_response_overrides()` brand-override pack merge. |
| `override_provider.py` | `OverrideProvider`/`NullOverrideProvider`. |
| `override_fixtures.py` | In-memory brand pack fixtures (stub/dev). |
| `salary_on_time/` | `pre_closure.yml` (54 KB), `on_due.yml`, `post_due.yml`. |
| `paisalo/` | `opener.yml`, `predue.yml`, `ondue.yml`, `postdue.yml`, `npa.yml`, `objections.yml`. |
| `test_generic/` | Generic test flows. |
| (root) | `compliance_handoff.yml`, `dispute.yml`, `followup.yml`, `hardship.yml`, `identity_verification.yml`, `identity_name_confirm.yml`, `payment_flows.yml`, `pay_now.yml`, `promise_to_pay.yml`, `refusal_negotiation.yml`, `robustness.yml`, `simple_ptp_test.yml`, `vulnerability.yml`, `reply_manifest.json` (27 KB). |

### Brain — `app/tenants/` (TenantRuntimeProfile YAML)

| File | One-liner |
|---|---|
| `salary_on_time.yml` | SOT profile: `flow_prefix=sot_`, on-rails flows, commit/push/reversal slots, blocked commands, deflection objections, pinned/dispute flows, coercion chain, cue packs, `voice_id=amit`, `tts_model=bulbul:v3`, `tts_pace=1.1`, `scenario_selector=due_date`, `reversal_target_flow=sot_obj_no_timeline`, `identity_slot=sot_identity_response`, `final_confirm_slot=sot_final_confirm`, `link_received_slot=sot_link_received`, `call_closed_slot=sot_call_closed`, `reason_slot=sot_payment_problem`. |
| `paisalo.yml` | PaisaLo profile: `flow_prefix=plo_`, on-rails flows (opener/predue/ondue/postdue1/2/3/npa), commit slots (plo_payment_intent/plo_timeline/plo_consent_2min/callback_window), `reversal_slots:[]` (no reversal stage yet), `callback_flow=plo_obj_callback_pd`, `respond_enabled=true`, `unknown_info_reply` (Hindi), 8-phrase `gate_allowlisted_phrases`, `scenario_selector=dpd_bucket_npa`, `identity_slot=plo_identity_response`, `call_closed_slot=plo_call_closed`, `reason_slot=plo_timeline`, cue packs (negation/id_yes/id_no/refusal/willing/willing_disqualifiers/callback_request/dispute_never_loan/dealer_payment/waiver_political/already_paid/new_loan/which_emi). |
| `test_generic.yml` | Generic test profile (no coercions). |

### Brain — `app/memory/` + `app/clients/` + `app/ws/` + `app/schemas/` + `app/sim/` + `app/util/`

| File | One-liner |
|---|---|
| `memory/store.py` | `create_memory_store()` composite (Upstash + Postgres + audit). |
| `memory/postgres_borrowers.py` | `PostgresBorrowerStore` phone lookup + load/save borrower. |
| `memory/upstash.py` | `UpstashClient` Redis REST for state TTL. |
| `memory/composite.py` | Composite memory wiring. |
| `memory/audit.py` | `TurnAuditChain`, `build_turn_audit_record()`. |
| `memory/pii.py` | PII slot keys. |
| `memory/test_borrower.py` | `hardcoded_test_borrower()` (SOT), `hardcoded_paisalo_borrower()` (PLO), `apply_test_borrower_slots()`. **Tenant-conditional: `state.tenant_id == "paisalo"` (lines 147, 158).** |
| `clients/orchestrator.py` | ari-orchestrator REST client (warm transfer, transfer status, hangup). |
| `clients/llm_vertex.py` | Vertex/Gemini client (stream + non-stream). |
| `clients/llm_groq.py` | Groq OpenAI-compatible client. |
| `clients/kb.py` | KB retrieval client (Fonada search API). |
| `clients/whatsapp.py` | WhatsApp template sender. |
| `clients/tools*.py` | Tools client (live/simulate/stub) + fixtures + SOT sim. |
| `ws/handler.py` | **60 KB `handle_brain_websocket()`.** session_start -> resolve -> turn loop -> chunk/done. Consult + conference-join watchers. **Tenant-conditional: `is_paisalo_test` (line 101), `test_force == "paisalo"` (line 1211), `test_tenant_id == "paisalo"` (line 1223).** |
| `ws/session.py` | `BrainWSSession` per-session state. |
| `ws/routing.py` | `resolve_session_tenant()`, `resolve_agent_routing()`. `FORCE_FLOW_ALIASES`: `salary-on-time -> (sot_opener, salary_on_time)`, `paisalo -> (plo_opener, paisalo)`. **Tenant-string hardcodes (lines 17-20, 84).** |
| `ws/borrower_resolve.py` | `resolve_session_borrower()`, `resolve_asr_language()`. |
| `ws/borrower_context.py` | `normalize_borrower_context()`, `apply_borrower_context_to_*`, `parse_tap_only()`. |
| `ws/chunking.py` | `chunk_reply_for_tts()`. |
| `ws/flow_class.py` | `flow_class_for_question_slot()`. |
| `ws/conference_transcript.py` | CF2.3 merged tap transcript store. |
| `ws/outbound_push.py` | `push_unsolicited_reply()`. |
| `ws/tenant_limits.py` | `SESSION_REGISTRY` + per-tenant concurrency cap. |
| `schemas/api.py` | `TurnRequest`, `TurnResponse`. |
| `schemas/command.py` | `Command` (start_flow/set_slot/clarify/respond/cancel_flow/human_handoff). |
| `schemas/flow.py` | `FlowSet`, `Flow`, `FlowStep`, `FlowBranch`, `ResponseTemplate` (with `attempt`, `tone_register`, `language`). |
| `schemas/state.py` | `ConversationState`, `BorrowerRecord`, `Frame`, `Event`. |
| `schemas/ws_contract.py` | WS message types (SessionStart/Turn/Cancel/Chunk/Done/Error/PlaybackDone/...). |
| `schemas/label_transition.py` | `Label`, `LabelStateModel`, `TransitionDecision`, `HIGH_RISK_LABELS`, `MONEY_PATH_LABELS`. |
| `schemas/manifest.py` / `overrides.py` / `compliance.py` / `decision.py` / `emotion.py` / `persona.py` / `recovery.py` | Supporting pydantic models. |
| `sim/runner.py` | SIM runner (scripted client simulator). |
| `sim/scripted_clients.py` | Scripted client definitions. |
| `util/phone.py` | Phone normalization. |

### Brain — `migrations/`

| File | One-liner |
|---|---|
| `001_borrowers.sql` | Base `borrowers` table (id, name, phone, amount_due, account_ref, language, tenant_id, created_at; UNIQUE(phone, tenant_id); indexes). Local test-stack only. |
| `002_paisalo_loan_fields.sql` | ALTER TABLE adds: repay_amount, loan_amount, due_date, disbursal_date, days_past_due, dpd, branch, branch_address, last_date_paid, product, npa_flag. Index idx_borrowers_tenant_dpd. |
| `seed_local_borrowers.sql` | Seeds B_RAJESH / B_PRIYA / B_AMIT (tenant=default). |
| `seed_paisalo_predue.sql` | Seeds PLO_RAMESH_PREDUE (+919810587857, dpd=-5, branch=Kanpur City, product=ABF). |

### Brain — `tests/`

| Subdir | Contents |
|---|---|
| `tests/unit/` | ~75 unit tests (test_api, test_turn_decision_log, test_nlg, test_tenant_profile, test_gate_allowlist, test_harden1_*, test_paisalo_hydration, test_sot_flow_pack, test_prompt_*, test_eb6_ws_contract, ...). |
| `tests/golden/` | ~30 golden/replay tests (test_executor_golden, test_paisalo_scenarios, test_plo_oof_p1_cue_packs / p2_reask_laddering / p3_grounding_forensic / p4_bside_wins / p5_committed_date, test_handle_turn_e2e, test_sot_pre_closure, ...). |
| `tests/sim/` | `test_release_gate.py`. |
| `tests/compliance/` | `test_release_gate.py`. |
| `tests/fixtures/` | JSON fixtures (after_hours, dynamic_dispute, dynamic_hardship, dynamic_ptp, happy_path_ptp, identity_name_confirm, identity_then_dispute, simple_ptp_test). |
| `tests/helpers/` | `brand_override_packs.py`, `test_borrowers.py`, `trust_blueprint_paths.py`. |

### Brain — `scripts/` (selection; ~180 files, mostly scratch)

Key: `apply_borrower_migrations.py`, `replay_eval.py`, `generate_reply_manifest.py`, `plo_outbound_live.py`, `sot_outbound_live.py`, `sot_test_run.py`, `sot_restore_mohali.py`, `seed_kb_flows.py`, `flow_sim.py`, `verify_dynamic_routing.py`. The rest are deploy/diag scratch (`_d4_*`, `_h1_*`, `_plo_oof_*`, `_r_*`, `_p*_`, `_tts_cache_*`).

### go-server — `Websocket/internal/media/` (selection)

| File | One-liner |
|---|---|
| `session.go` | Per-call media session registry; rates pinned `session=sarvam=asr=8000`; forwards `client_id` verbatim. |
| `sarvam_asr.go` | Sarvam ASR streaming WS (saaras:v3, hi-IN, VAD, interim/final). |
| `sarvam_tts.go` | Sarvam TTS WS (bulbul:v3, voice_id from session). |
| `buffer.go` | Audio buffer + chunker (16-bit PCM @8kHz). |
| `wsclient.go` | Sarvam WS dial + reconnect. |
| `asr_buffer.go` | ASR partial buffer. |
| `tts_cache.go` | TTS audio cache (SHA256 keyed). |

### go-server — `Websocket/internal/brain/`

| File | One-liner |
|---|---|
| `client.go` | EB-6 brain WS client. `ConfigFromEnv` reads `BRAIN_TENANT_ID` (used as `client_id` for media-meta). |
| `contract.go` | Go->brain + brain->Go message contracts. `SessionStartPayload{TenantID, ClientID}` is the source of truth on BYO/media-meta path. |
| `session.go` | Brain WS session manager (send lock, inflight turn id). |

### go-server — `Websocket/internal/` (other)

| File / Dir | One-liner |
|---|---|
| `config/` | Env-driven config (`Config`, `Media`, `Brain`, `Sarvam`, `Server`). |
| `server/` | HTTP/WS server bootstrap; `/media` + `/healthz`. |
| `tap/` | CF2.3 merged-tap transcript store. |
| `log/` | Structured logger. |

### go-server — `Websocket/cmd/`

`cmd/server/main.go` — entrypoint; wires config + media + brain + tap.

### connector — `asterisk-connector/internal/`

| File | One-liner |
|---|---|
| `wsclient/wsclient.go` | Fonada binary-PCM WS media contract. `AudioSpec` (default `slin16`) + `G711AudioSpec` (8kHz). `sessionStart` payload. |
| `bridge/bridge.go` | AudioSocket->media-WS bridge. `realDialer` connects `wsclient` with `clientID`. |
| `config/config.go` | Connector env config. `AudioSocketListener{..., Tenant}` — `Tenant` is used as `client_id`. |
| `audiosocket/` | AudioSocket listener + frame parser. |
| `ari/` | ARI bridge (originate, hangup, get channel). |
| `log/` | Logger. |

### orchestrator — `ari-orchestrator/`

Asterisk ARI app: originate, bridge, hangup, warm-transfer, channel state. Exposes REST consumed by `app/clients/orchestrator.py`. Not scanned in depth for this audit (boot spec scopes connector + go-server internal only).

---

## 2. TURN LIFECYCLE WALK (file:function refs)

For a single user utterance arriving over the brain WebSocket:

1. **session_start** — `ws/handler.py:handle_brain_websocket` receives `session_start` payload. Calls `ws/routing.py:resolve_session_tenant` (tenant_id from payload, alias map, env override) and `ws/borrower_resolve.py:resolve_session_borrower` (phone -> borrower record via `memory/postgres_borrowers.py:PostgresBorrowerStore.find_by_phone`). In `TEST_MODE=true`, `ws/handler.py:_normalize_test_session_start` injects `test_tenant_id` / `test_force` (tenant-conditional: paisalo branch at line 101/1211/1223). `ws/session.py:BrainWSSession` is created and registered in `ws/tenant_limits.py:SESSION_REGISTRY` (per-tenant concurrency cap).

2. **turn input** — `ws/handler.py` reads `turn` message, calls `app/engine/turn.py:handle_turn(state, transcript, ...)`.

3. **state load + hydrate** — `turn.py:handle_turn` loads `ConversationState` from `memory/store.py` (Upstash), then `app/engine/tracker.py:hydrate_from_borrower` injects borrower facts (amount_due, due_date, dpd, branch, ...). `app/engine/followup.py:hydrate_followup_from_borrower` adds active followups. `ws/borrower_context.py:normalize_borrower_context` normalizes any client-supplied context.

4. **safety preempt** — `app/engine/safety.py:safety_preempt` scans the inbound transcript for vulnerability / distress / critical-inbound cues. If matched, returns an early care-first reply and skips the rest of the pipeline.

5. **closed-call early-exit** — `turn.py:_run_closed_early_exit` checks `call_closed` slot; if set, returns a polite close and ends the call.

6. **retrieval / catalog** — `app/engine/retrieval.py:retrieve_flow_candidates` (KB RAG, legacy/digression path) and `app/engine/catalog.py:tenant_flow_catalog` (full on-rails catalog from `TenantRuntimeProfile`). `catalog_mode=true` skips KB scores and trusts the catalog.

7. **command_gen** — `app/engine/command_gen.py:transcript_to_commands` calls the LLM (Groq `llm_groq.py` or Vertex `llm_vertex.py`) with the system prompt, transcript, hydrated slots, and the catalog. Emits `Command[]` (start_flow / set_slot / clarify / respond / cancel_flow / human_handoff) + `rejections` + `raw`.

8. **coercion chain (Tier-1)** — `app/engine/scripted_coercions.py:run_coercion_chain` runs the profile-driven chain: `dispute -> callback -> willing -> refusal -> identity -> reversal -> confirm -> link -> committed_date -> reason_catchall`. Each coercer inspects the transcript for tenant-specific cue packs (from `TenantRuntimeProfile.cue_packs`) and may rewrite/insert commands. `INABILITY_RE` is shared. `coerce_committed_date` extracts a date and stores it in `profile.committed_date_slot` (SOT: `sot_commit_timing`; PLO: `plo_timeline`).

9. **slot validation** — `app/engine/slot_validation.py:validate_commands` drops `set_slot` commands that would overwrite hydrated facts or that carry wrong-type answers.

10. **Label Transition Layer (LTL)** — `app/engine/label_transition.py:run_label_transition` rewrites commands based on labels (link_not_received, ownership_confirm, loan_not_taken, wrong_amount, already_paid, legal). Shadow mode logs; enforce mode (SOT only) rewrites. `SOT_LABEL_TO_FLOW` / `SOT_FLOW_TO_LABEL` maps drive SOT-specific routing.

11. **tracker.apply** — `app/engine/tracker.py:apply(state, events)` mutates `ConversationState` (frames stack, slots, robustness counters). `app/engine/robustness.py:track_frustration` / `track_slot_reask` update counters.

12. **priority reorder** — `app/engine/priority.py:reorder(state, flows)` reorders the frame stack so identity > safety > dispute_hold > collection > followup.

13. **decision overlay (Phase-2 shadow)** — `app/engines_p2/decision_overlay.py:apply_decision_overlay` adjusts routing based on risk/trust/emotion (shadow only, no side-effects yet).

14. **executor** — `app/engine/executor.py:run_async(state, flows, action_runner)` walks the top frame's flow stack. Each step is one of `collect` (ask a slot), `action` (call `actions.py`), `decide` (branch on slots), `utter` (emit a reply), `end` (terminate flow). Returns `ExecResult{reply_id, utter_chain, question_slot, end_call, disposition}`. Step cap 256.

15. **action execution** — `app/engine/actions.py:make_async_action_runner` returns the runner the executor calls. Tenant-specific actions: `select_sot_scenario`, `select_plo_scenario`, `plo_chain_*`, `sot_*`/`plo_*` resetters, `classify_sot_relation`, `activate_sot_restricted_mode`. Generic actions: `set_slot`, `start_flow`, `hangup_call`, `transfer_call`, `send_whatsapp_message`, `start_recording`, `pause_recording`, `resume_recording`.

16. **NLG** — `app/engine/nlg.py:draft_reply_resolved` resolves the reply_id against `reply_manifest.json` + brand overrides, then `render_resolved` interpolates slots. `spoken_amount_hindi` / `spoken_date_hindi` / `spoken_days_hindi` produce spoken-form Hindi. `render_short_reask` handles the re-ask ladder (P2). `COLLECT_SLOT_REPLY_IDS` / `CLARIFY_REASK_REPLY_IDS` map slot -> reply_id (tenant-keyed).

17. **identity gate** — `app/engine/identity_gate.py:apply_identity_entry_gate` injects the `identity_verification` frame before any collection flow if identity is not yet confirmed. `defer_collection_flows` parks collection frames. `must_block_debt_disclosure` + `reply_discloses_debt` inspect the drafted reply for debt phrases; if blocked, `slots_for_nlg` strips debt slots and the reply is replaced with a care-first clarification.

18. **respond guard (Tier-3)** — `app/engine/respond_guard.py:ground_respond_text` grounds any `respond` text against hydrated slots and caps at 220 chars. Returns `(grounded, grounding_result)`; if grounding fails, the respond is downgraded to a clarify.

19. **compliance gate** — `app/engine/gate.py:gate(reply, state, tenant_cfg)` runs the 4-level gate: critical_inbound -> vulnerable -> opt_out -> third_party/pre-identity debt disclosure -> dunning_suppressed -> call_window -> attempt_cap -> dispute_hold -> prohibited. Returns `GateResult{verdict, text, level, reason, warnings, transfer_to_human}`. `gate_allowlisted_phrases` from `TenantRuntimeProfile` (PaisaLo has 8) override pressure-phrase flags.

20. **chunk for TTS** — `ws/chunking.py:chunk_reply_for_tts` splits the gated reply into sentence chunks for streaming TTS.

21. **persist** — `turn.py:_persist_turn` writes `ConversationState` back to Upstash (TTL), writes the borrower record back to Postgres, appends to the audit chain (`memory/audit.py:TurnAuditChain`), and calls the various `sync_*_on_persist` hooks (`compliance_handoff`, `dispute_breadth`, `followup`, `hardship`, `refusal_negotiation`, `engines_p2/*`).

22. **outbound** — `ws/handler.py:_push_reply` sends chunked reply frames back to the go-server; the go-server forwards to Sarvam TTS and streams PCM to Asterisk.

23. **decision log** — `app/engine/turn_decision_log.py:log_turn_decision` dumps the full per-turn decision (transcript, candidates, commands, guards, gate verdict) to the decision-log channel for replay/debug.

24. **side-effects (post-gate)** — `turn.py` triggers warm-transfer (`clients/orchestrator.py`), WhatsApp background send (`clients/whatsapp.py`), or call close — all post-gate, per the v1.3 "gate-before-side-effect" rule.

---

## 3. COMPLETE TENANT-CONDITIONAL INVENTORY

Every hardcoded `salary_on_time` / `paisalo` / `sot_` / `plo_` reference in engine code, with file:line, what it special-cases, and whether a `TenantRuntimeProfile` field could replace it.

### Counts (matches per file, regex `sot_|plo_|salary_on_time|paisalo`)

| File | Matches | Boot-spec hint | Status |
|---|---|---|---|
| `app/engine/turn.py` | 78 | ~32 | **Higher than hint** — many `sot_*` are local var names, not branch points. See breakdown below. |
| `app/engine/nlg.py` | 30 | ~30 | Matches hint. All are slot->reply_id dict entries (data, not branch points). |
| `app/config.py` | 27 | ~5 | **Higher than hint** — most are `Settings` field defaults (data, declarative). Only 1 is a real branch. |
| `app/engine/actions.py` | 70+ | (not hinted) | All `sot_*`/`plo_*` action names + slot keys (data, dispatched by name). |
| `app/engine/scripted_coercions.py` | 7 | (not hinted) | 2 real branches on `profile.flow_prefix == "sot_"`. |
| `app/engine/label_transition.py` | 14 | (not hinted) | SOT-only enforce adapter + `SOT_LABEL_TO_FLOW` map (data). |
| `app/engine/identity_gate.py` | 2 | (not hinted) | 1 hardcoded `sot_opener` flow name in `IDENTITY_FLOWS`. |
| `app/ws/handler.py` | 9 | (not hinted) | 3 real branches on `is_paisalo_test` / `test_force == "paisalo"`. |
| `app/ws/routing.py` | 50+ | (not hinted) | `FORCE_FLOW_ALIASES` map + `ALLOWED_OPENERS` list (data). |
| `app/memory/test_borrower.py` | 12 | (not hinted) | 2 real branches on `state.tenant_id == "paisalo"`. |

### Real branch points (would need a `TenantRuntimeProfile` field to replace)

| File:line | Code | Special-cases | Profile-able? |
|---|---|---|---|
| `app/config.py:610` | `if tenant_id in {"salary_on_time", "paisalo"}:` | The main `tenant_config()` branch — only these two tenants get a `TenantConfig`; everyone else gets defaults. | **Partly.** The set itself should be "all tenants with a YAML profile" — replace with `if get_tenant_profile(tenant_id).exists:`. |
| `app/engine/turn.py:931` | `and request.tenant_id != "paisalo"` | In test mode, SOT-test-mode is gated off when tenant is paisalo (so PLO test path runs instead). | **Yes** — `profile.allow_sot_test_mode` bool. |
| `app/engine/turn.py:941` | `elif settings.test_mode and request.tenant_id == "paisalo" and plo_override:` | Pick `hardcoded_paisalo_borrower` for PLO test. | **Yes** — `profile.test_borrower_factory` ref. |
| `app/engine/scripted_coercions.py:243` | `if not supplied_time and profile.flow_prefix != "sot_":` | SOT-only `coerce_committed_date` early-return for non-SOT tenants. | **Yes** — `profile.supports_committed_date_coercion` bool. |
| `app/engine/scripted_coercions.py:276` | `if profile.flow_prefix != "sot_":` | SOT-only timing-slot set (`sot_customer_time`, `sot_commit_timing`). | **Yes** — `profile.timing_slot_set` list. |
| `app/engine/label_transition.py:121` | `if tenant_id == "salary_on_time":` | SOT-only enforce-mode adapter wiring. | **Yes** — `profile.ltl_enforce_enabled` bool (already implied by `ltl_mode: enforce` in YAML). |
| `app/engine/identity_gate.py:120` | `"sot_opener"` in `IDENTITY_FLOWS` | Hardcoded SOT opener flow name in identity bypass list. | **Yes** — `profile.identity_bypass_flows` list. |
| `app/ws/handler.py:101` | `is_paisalo_test = ... == "paisalo"` | Pick PLO vs SOT test borrower/agent_id. | **Yes** — `profile.test_agent_id` + `profile.test_borrower_id`. |
| `app/ws/handler.py:1211` | `if test_force == "paisalo"` | Force PLO test agent_id. | Same as above. |
| `app/ws/handler.py:1223` | `if (settings.test_tenant_id or "") == "paisalo"` | Same. | Same as above. |
| `app/memory/test_borrower.py:147` | `keys = _PLO_LOAN_KEYS if state.tenant_id == "paisalo" else _SOT_LOAN_KEYS` | Pick loan-key set for test-borrower slot hydration. | **Yes** — `profile.test_loan_keys` list. |
| `app/memory/test_borrower.py:158` | `if state.tenant_id == "paisalo":` | Force PLO scenario override slot. | **Yes** — `profile.test_scenario_override_slot` str. |

### Data-only references (NOT branch points — already declarative, just tenant-keyed)

- `app/engine/nlg.py:35-86` — `COLLECT_SLOT_REPLY_IDS` and `CLARIFY_REASK_REPLY_IDS` dict entries. Slot name -> reply_id. Already data; the dict is the lookup table. No profile change needed; the dict could itself be moved into `TenantRuntimeProfile.collect_slot_reply_ids` if we want per-tenant isolation, but the current shared-dict-with-tenant-keys design works.
- `app/engine/nlg.py:91` — `CLARIFY_MIN_ROTATION_SLOTS = frozenset({"sot_identity_response"})`. **Profile-able**: `profile.clarity_min_rotation_slots`.
- `app/engine/actions.py:184-217` — action-name allowlist (string constants). Already data; the dispatcher matches by string. No profile change needed.
- `app/engine/actions.py:762-1049` — `if action == "sot_chain_*"` / `plo_chain_*` branches. These are dispatch by name, not by tenant. The action *names* are tenant-specific but the dispatch is generic. **Profile-able only if** we want to disallow non-tenant tenants from calling `sot_*` actions — `profile.allowed_actions` set.
- `app/engine/actions.py:994-1014` — `_plo_voices` / `_plo_pace` dicts (scenario -> voice_id / pace). **Profile-able**: `profile.scenario_voice_map` / `profile.scenario_pace_map`.
- `app/engine/label_transition.py:45-52` — `SOT_LABEL_TO_FLOW` map. **Profile-able**: `profile.label_to_flow_map`.
- `app/engine/label_transition.py:157-177` — `sot_payment_intent` / `sot_identity_response` / `sot_knows_customer` / `sot_link_received` slot-name checks inside `_slot_state_transition`. **Profile-able**: `profile.slot_state_transitions` dict.
- `app/ws/routing.py:17-20` — `FORCE_FLOW_ALIASES` map. **Profile-able**: `profile.force_flow_alias` str.
- `app/ws/routing.py:32-68` — `ALLOWED_OPENERS` list. **Profile-able**: `profile.allowed_openers` list (already partly in YAML as `on_rails_flows`).
- `app/ws/routing.py:84` — `"salary-on-time": "salary_on_time"` canonicalization. **Profile-able**: `profile.canonical_aliases` list.
- `app/config.py:132, 142, 146, 201, 213, 222, 228, 236, 390, 392, 395, 397, 437, 525-529` — `Settings` fields + `_TENANT_ROUTING_DEFAULTS` entries. All declarative env-driven config. The `sot_*` field names are SOT-specific but they are *settings*, not branches. **Profile-able**: most could move to `TenantRuntimeProfile` (e.g. `sot_digression_enabled` -> `profile.digression_enabled`, `sot_pinned_flows` -> `profile.pinned_flows`, `sot_flow_confidence_floor` -> `profile.flow_confidence_floor`, `sot_dispute_flows` -> `profile.dispute_flows`, `sot_dispute_evidence_bar` -> `profile.dispute_evidence_bar`, `sot_frustration_escalate_turns` -> `profile.frustration_escalate_turns`). This is the W2-A2 config-deprecation target.
- `app/engine/turn.py:264-362` — `_coerce_sot_*` wrapper functions. These are thin pass-throughs to `scripted_coercions.py` kept for test-import back-compat. **Profile-able**: not branches; delete once tests import directly from `scripted_coercions`.
- `app/engine/turn.py:489` — `else _sot_dispute_flow(transcript)` — calls SOT-specific dispute flow resolver. Already profile-driven via `_sc.dispute_flow(transcript, _sot_profile())`. The `_sot_profile()` hardcode is the only tenant-string literal; **profile-able**: replace with `profile = get_tenant_profile(state.tenant_id)`.
- `app/engine/turn.py:926-944` — `sot_override` / `plo_override` test-mode borrower selection. Real branch (see above).
- `app/engine/turn.py:1029` — `or "sot_call_closed"` — fallback slot name for closed-call detection. **Profile-able**: `profile.call_closed_slot` (already in YAML).
- `app/engine/turn.py:1079-1287` — `sot_awaiting_slot`, `sot_on_rails`, `sot_closed`, `sot_blank_transcript`, `sot_digression`, `sot_blocked_commands` local vars. **Not branches** — these are local var names that happen to be `sot_`-prefixed because the whole block was written for SOT and never generalized. The block *is* effectively SOT-only: it runs for every tenant but the `sot_digression_enabled` setting gates the digression path (SOT-only). **Profile-able**: rename to `awaiting_slot` / `on_rails` / etc. and gate digression via `profile.digression_enabled`.
- `app/engine/turn.py:1224-1250` — `sot_digression` / `sot_flow_confidence_floor` / `sot_dispute_evidence_bar` settings reads. **Profile-able**: move to `profile.flow_confidence_floor` / `profile.dispute_evidence_bar`.
- `app/engine/turn.py:1566, 1591, 1595` — comments + `select_plo_scenario` reference + `sot_call_closed` persist note. Comments only; the `sot_call_closed` is `profile.call_closed_slot`.

### Summary

- **Real branch points: 12.** All 12 are profile-able with new `TenantRuntimeProfile` fields (mostly bools or lists).
- **Data-only references: ~200.** Already declarative (dicts, lists, settings). Most can move into `TenantRuntimeProfile` for per-tenant isolation, but the current shared-dict design works for the two-tenant world.
- **The big W2-A2 win** is moving the 6 `sot_*` `Settings` fields (`sot_digression_enabled`, `sot_pinned_flows`, `sot_flow_confidence_floor`, `sot_dispute_flows`, `sot_dispute_evidence_bar`, `sot_frustration_escalate_turns`) into `TenantRuntimeProfile`. That alone removes the SOT-specificity from `turn.py`'s digression/dispute block and makes the block tenant-agnostic.

---

## 4. CONFIG / ENV INVENTORY (refresh of audit A2)

### `app/config.py:Settings` — env-driven

| Field | Env var | Default | Used by |
|---|---|---|---|
| `test_mode` | `TEST_MODE` | `False` | `ws/handler.py`, `turn.py` — gates hardcoded test borrower + test agent_id. |
| `test_tenant_id` | `TEST_TENANT_ID` | `"salary_on_time"` | `ws/handler.py` — default test tenant. |
| `test_sot_scenario` | `TEST_SOT_SCENARIO` | `""` | `turn.py:926`, `memory/test_borrower.py:79` — override SOT scenario. |
| `test_plo_scenario` | `TEST_PLO_SCENARIO` | `""` | `turn.py:927`, `memory/test_borrower.py:110` — override PLO scenario. |
| `sot_digression_enabled` | `SOT_DIGRESSION` | `False` | `turn.py:1102` — CALM-style digression (SOT-only). |
| `sot_pinned_flows` | `SOT_PINNED_FLOWS` | `"sot_obj_link_request,sot_obj_diff_number_link,sot_obj_wrong_amount,sot_obj_already_paid_q"` | `turn.py:1124` — NevIR pin list. |
| `sot_flow_confidence_floor` | `SOT_FLOW_FLOOR` | `0.6` | `turn.py:1229` — KB score floor for digression. |
| `sot_dispute_flows` | `SOT_DISPUTE_FLOWS` | `"sot_obj_never_loan,sot_obj_wrong_amount,sot_obj_death,sot_obj_frozen_account"` | `turn.py:1244` — dispute candidate set. |
| `sot_dispute_evidence_bar` | `SOT_DISPUTE_BAR` | `2` | `turn.py:1250` — dispute evidence threshold. |
| `sot_frustration_escalate_turns` | `SOT_FRUSTRATION_ESCALATE_TURNS` | (int) | `engines_p2/*` — frustration escalation. |
| `upstash_*` | `UPSTASH_*` | — | `memory/upstash.py` — state store. |
| `postgres_*` | `POSTGRES_*` | — | `memory/postgres_borrowers.py` — borrower store. |
| `groq_api_key` / `groq_model` | `GROQ_API_KEY` / `GROQ_MODEL` | — | `clients/llm_groq.py` — Tier-2/Tier-3 LLM. |
| `vertex_project` / `vertex_location` | `VERTEX_PROJECT` / `VERTEX_LOCATION` | — | `clients/llm_vertex.py` — Tier-3 LLM. |
| `kb_*` | `KB_*` | — | `clients/kb.py` — KB RAG. |
| `whatsapp_*` | `WHATSAPP_*` | — | `clients/whatsapp.py` — WhatsApp bg send. |
| `orchestrator_*` | `ORCHESTRATOR_*` | — | `clients/orchestrator.py` — ari-orchestrator REST. |
| `tools_mode` | `TOOLS_MODE` | `"stub"` | `clients/tools.py` — live/simulate/stub. |
| `ltl_mode` | `LTL_MODE` | `"shadow"` | `engine/label_transition.py:118` — shadow/enforce. |
| `respond_enabled` | `RESPOND_ENABLED` | `False` | `engine/command_gen.py` — Tier-3 respond gating. |
| `catalog_mode` | `CATALOG_MODE` | `True` | `engine/command_gen.py` — skip KB scores, trust catalog. |
| `voice_id` / `tts_model` / `tts_pace` | `VOICE_ID` / `TTS_MODEL` / `TTS_PACE` | — | `ws/handler.py` — default TTS, overridden by `TenantRuntimeProfile` and `select_*_scenario` actions. |
| `gate_allowlisted_phrases` | (none — YAML only) | — | `engine/gate.py` — per-tenant pressure-phrase allowlist (PaisaLo has 8). |

### `_TENANT_ROUTING_DEFAULTS` (config.py:525)

- `salary_on_time`: `default_locale=hi-IN`, `opener_fallback_reply_id=sot_greeting`.
- `paisalo`: `default_locale=hi-IN`, `default_agent_id=paisalo-test`, `opener_fallback_reply_id=plo_greeting_unknown`.

### `_TENANT_RATE_LIMITS` (config.py:437)

- `paisalo`: `max_attempts_per_day=200`.

### go-server env (`Websocket/internal/brain/client.go:ConfigFromEnv`)

- `BRAIN_TENANT_ID` — used as `client_id` for media-meta path (BYO).
- `BRAIN_WS_URL`, `BRAIN_TIMEOUT`, `BRAIN_RECONNECT_*`.

### connector env (`asterisk-connector/internal/config/config.go`)

- `AudioSocketListener.Tenant` — used as `client_id` (matches brain's `BRAIN_TENANT_ID`).
- `AudioSocketListener.Addr`, `MediaServerURL`, `ARI_*`.

---

## 5. DATA MODEL

### `borrowers` table (migrations/001 + 002)

| Column | Type | Source | Notes |
|---|---|---|---|
| `id` | text PK | 001 | Borrower ref. |
| `name` | text | 001 | Customer name. |
| `phone` | text | 001 | E.164. UNIQUE with tenant_id. |
| `amount_due` | numeric | 001 | SOT: total due. PLO: repay_amount overrides. |
| `account_ref` | text | 001 | Loan/account ref. |
| `language` | text | 001 | hi-IN default. |
| `tenant_id` | text | 001 | `salary_on_time` / `paisalo` / `default`. |
| `created_at` | timestamptz | 001 | — |
| `repay_amount` | numeric | 002 (PLO) | PLO repayment amount. |
| `loan_amount` | numeric | 002 (PLO) | PLO principal. |
| `due_date` | date | 002 (PLO) | PLO due date. |
| `disbursal_date` | date | 002 (PLO) | PLO disbursal. |
| `days_past_due` | int | 002 (PLO) | PLO DPD. |
| `dpd` | int | 002 (PLO) | PLO DPD bucket. |
| `branch` | text | 002 (PLO) | PLO branch name. |
| `branch_address` | text | 002 (PLO) | PLO branch address. |
| `last_date_paid` | date | 002 (PLO) | PLO last payment. |
| `product` | text | 002 (PLO) | PLO product (ABF, ...). |
| `npa_flag` | bool | 002 (PLO) | PLO NPA flag. |

Indexes: `UNIQUE(phone, tenant_id)`, `idx_borrowers_tenant_dpd`.

### `media_streams` (go-server, in-memory)

Per-call session registry (`Websocket/internal/media/session.go`):
- `session_id` (UUID)
- `client_id` (tenant string, forwarded verbatim from connector)
- `phone` / `borrower_id`
- `voice_id` / `tts_model` / `tts_pace` (from `TenantRuntimeProfile` + `select_*_scenario`)
- `asr_language` (hi-IN default)
- `rates`: `session=sarvam=asr=8000` (pinned)
- `tap_enabled` (CF2.3 merged-tap)

### Slots taxonomy (per `TenantRuntimeProfile`)

**SOT (`salary_on_time.yml`):**
- Identity: `sot_identity_response`, `sot_knows_customer`, `sot_relation_type`, `sot_sibling_type`, `sot_tp_class`, `sot_restricted_followup`, `sot_no_detail`.
- Commit: `sot_payment_intent` (1-5), `sot_payment_problem`, `sot_commit_timing` (today/tomorrow/before_due/on_due/after_due), `sot_customer_time`, `sot_final_confirm`.
- Scenario: `sot_scenario` (pre/on_due/post_due), `sot_ondue_decision`, `sot_afterdue_decision`.
- Link: `sot_link_received`.
- Close: `sot_call_closed`.
- Claims: `sot_claims_paid` (action-set).

**PLO (`paisalo.yml`):**
- Identity: `plo_identity_response`.
- Commit: `plo_payment_intent`, `plo_timeline`, `plo_consent_2min`, `callback_window`.
- Scenario: `plo_scenario` (predue/ondue/postdue1/2/3/npa), `plo_scenario_override`.
- Close: `plo_call_closed`.
- Reset: `plo_reset_timeline`, `plo_reset_payment_intent` (actions).

### `FACT_SLOTS_FOR_RESPOND` (Tier-3 grounding allowlist)

Slots that may be cited in a `respond` text without grounding failure. Drawn from hydrated borrower facts:
- `amount_due`, `repay_amount`, `loan_amount`, `due_date`, `dpd`, `days_past_due`, `branch`, `branch_address`, `last_date_paid`, `product`, `npa_flag`, `account_ref`, `name`, `phone`, `language`.

Slots NOT in this set (e.g. `sot_payment_intent`, `plo_timeline`) are commitment slots — citing them in a `respond` is a grounding failure because they are borrower-asserted, not borrower-fact.

---

## 6. KNOWN-DEBT APPENDIX (from tracker)

Imported from `IMPLEMENTATION_TRACKER_V2.md` "Known Debt" register (IDE-buffer content, since on-disk file is 0-byte):

| ID | Component | Debt | Owner | Phase |
|---|---|---|---|---|
| DEBT-001 | `engine/turn.py` | `_coerce_sot_*` wrappers (lines 264-362) kept for test-import back-compat. Delete once tests import from `scripted_coercions` directly. | impl | W2-A2 |
| DEBT-002 | `engine/turn.py` | `sot_*` local var names in digression block (lines 1079-1287) — block is effectively SOT-only. Rename + gate via `profile.digression_enabled`. | impl | W2-A2 |
| DEBT-003 | `config.py` | 6 `sot_*` `Settings` fields should move to `TenantRuntimeProfile`. | impl | W2-A2 |
| DEBT-004 | `engine/label_transition.py` | SOT-only enforce adapter (line 121). Generalize via `profile.ltl_enforce_enabled`. | impl | W2-A2 |
| DEBT-005 | `engine/identity_gate.py:120` | Hardcoded `sot_opener` in `IDENTITY_FLOWS`. Move to `profile.identity_bypass_flows`. | impl | W2-A2 |
| DEBT-006 | `ws/handler.py` | 3 `paisalo` test-mode branches (lines 101, 1211, 1223). Move to `profile.test_agent_id` / `profile.test_borrower_id`. | impl | W2-A2 |
| DEBT-007 | `ws/routing.py:17-20` | `FORCE_FLOW_ALIASES` hardcoded SOT/PLO strings. Move to `profile.force_flow_alias`. | impl | W2-A2 |
| DEBT-008 | `memory/test_borrower.py:147, 158` | 2 `paisalo` branches. Move to `profile.test_loan_keys` / `profile.test_scenario_override_slot`. | impl | W2-A2 |
| DEBT-009 | `engine/nlg.py` | `COLLECT_SLOT_REPLY_IDS` / `CLARIFY_REASK_REPLY_IDS` shared dict with tenant-keyed entries. Consider per-tenant isolation via `profile.collect_slot_reply_ids`. | impl | W3 |
| DEBT-010 | `engine/actions.py:994-1014` | `_plo_voices` / `_plo_pace` dicts hardcoded. Move to `profile.scenario_voice_map` / `profile.scenario_pace_map`. | impl | W2-A2 |
| DEBT-011 | `migrations/002_paisalo_loan_fields.sql` | PLO-specific columns on the shared `borrowers` table. Long-term: per-tenant borrower tables or JSONB column. | architect | W4 |
| DEBT-012 | `engine/turn.py:926-944` | Test-mode borrower selection branches on `tenant_id == "paisalo"`. Move to `profile.test_borrower_factory`. | impl | W2-A2 |
| DEBT-013 | `scripts/` | ~180 untracked scratch scripts. Triage + delete or move to `scripts/scratch/`. | impl | W4 |
| DEBT-014 | `tests/golden/` | 8 modified golden transcripts in working tree (test-order pollution). Re-record clean. | impl | W2-4 |
| DEBT-015 | `docs/` | Chat-only docs (CURSOR_BOOT_V2, MASTER_SPRINT_PLAN, W2_SPRINT_SPEC, PAISALO_FRAGMENT_LIBRARY_V1, VOICE_STACK_ARCHITECTURE, CURSOR_HANDOVER) are 0-byte on disk; content lives in IDE buffer only. CURSOR_HANDOVER is missing entirely. | user | P0 |

---

## 7. PHASE 0 SIGN-OFF

- **P0 bar:** 100% — `docs/REPO_CONTEXT.md` written, tracker updated.
- **Status:** [R] (ready for architect review).
- **Stop point:** Do NOT start Phase A2 until the architect signs off.
- **Outstanding for user:** (a) save the 6 chat-only docs from IDE buffer to disk so they can be committed; (b) create `CURSOR_HANDOVER.md` from the architect's chat history (the "8 live-bug classes" are not in the repo).







