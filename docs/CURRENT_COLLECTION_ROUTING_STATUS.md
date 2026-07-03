# Current Collection Routing Status

> Read-only codebase discovery report. No application code was modified.
> Scope: `Collection/` (Python FastAPI "brain") + `Websocket/` (Go media server) at `c:\Users\nitis\source\repos\Main`.
> Line numbers are given where available and reflect the code at time of writing; treat them as close references, not guarantees.

## 1. Executive Summary

| Question | Verdict |
|----------|---------|
| Pipeline understood | **Yes** (ASR → Go media server → brain WS `/ws/brain` → `handle_turn` → gate → chunks → TTS) |
| Explicit label transition layer found | **No** (verdict #3 below — only flow switching + indirect equivalents) |
| Flow switching support found | **Yes** (flow stack, priority reorder, CALM digression, coercions) |
| Evidence accumulator found | **Yes (scoped)** — cross-turn dispute accumulator; frustration counter |
| High-risk guard found | **Yes** — deterministic dispute matcher, pinned flows, confidence floor, safety pre-empt, compliance gate |
| Extra routing features found | **Yes** (emotion/risk/trust/persona engines, identity gate, repair layer, decision overlay, barge-in/supersede) |

**Most important findings:**

1. The brain is a single orchestration function — `handle_turn` in `Collection/app/engine/turn.py` (~L1005–1564) — running the ordered pipeline `load → terminal guard → safety → retrieval → command_gen (LLM) → SOT coercions → slot validation → clarify → confidence floor → dispute accumulator → tracker.apply → priority.reorder → executor → transfer/whatsapp hooks → repair → NLG → compliance gate → persist`.
2. Routing is **retrieval-constrained**: the LLM only emits structured commands (`start_flow/set_slot/cancel_flow/clarify`, `human_handoff`/`cannot_handle` blocked for SOT) over a JSON schema whose flow-enum is seeded from KB candidates + pinned flows (`command_gen.py`).
3. Flow selection is governed by a **priority ladder** (`priority.py`: `opt_out > vulnerable > identity > dispute > hardship > ptp > refusal > reminder`) that reorders the `flow_stack` and parks lower-priority frames after every command batch.
4. There are **five+ deterministic SOT coercions** (`turn.py`) that override the LLM for high-stakes/awaited-slot cases: dispute matcher, push-willing, identity yes/no, commit-reversal, final-confirm, and link-received.
5. Three explicit "layers" address the historical wrong-routing bug: **Layer 0 pinned flows**, **Layer 3 confidence floor** (`SOT_FLOW_FLOOR=0.6`), and a **cross-turn dispute evidence accumulator** (`SOT_DISPUTE_BAR=2`).
6. **State (`ConversationState`) is deliberately minimal**: `slots` (untyped dict), `flow_stack` (LIFO of `Frame`), `events`, `attempts`, `version`. There is **no `active_label`/`previous_label`/`unresolved_high_risk_label`** field.
7. Durable cross-call memory lives on `BorrowerRecord` (Postgres + Upstash/in-memory) and carries **trust / risk / persona history with decay** — the closest thing to label transition tracking that exists today (persona transitions logged in `persona_history`).
8. Evidence accumulation exists but is **narrow** (dispute themes + frustration streak), stored inside `slots`, and has **no time-decay**; risk/trust engines do have decay but run mostly off the hot path.
9. Terminal/close handling is robust: `hangup_call`/`transfer_call` set `end_call` + `sot_call_closed`; a turn-start terminal guard and a flow-exhaustion guard force disconnect.
10. Compliance gate (`gate.py`) enforces call window, opt-out, debt-disclosure, attempt caps, prohibited phrases on the **outbound** reply; safety pre-empt (`safety.py`) can short-circuit on **inbound** distress. Note SOT sets `enforce_compliance_gate=false` for the tenant.
11. The Go server (`Websocket/`) handles carrier audio → denoise → AMD → Sarvam ASR → semantic-turn EOU/barge-in → brain WS → ElevenLabs/Sarvam TTS → carrier. It does **not** make routing decisions (only `flow_class` endpointing hints) and does **not** implement transfer (brain-only).
12. Barge-in and turn superseding are real: the latest caller transcript cancels/merges the in-flight brain turn (`ws/session.py`, Go `client.go`).
13. Test coverage is strong at the function level (coercions, layers, accumulator, link flow) and for SOT golden paths, but **thin on multi-turn e2e** for cross-intent transitions (refusal→link, wrong-person→link, accumulator through full `handle_turn`).
14. Several flags are **defined but not wired**: `enforce_safety_gate` (TenantConfig), `SOT_DEFLECTION_OBJECTIONS` (only used in digression-off path), `WS_TURN_DEADLINE_MS` / `force_flow` (absent from Go), local VAD `ObserveAudio` (not in production chain).
15. **No unified intent/label taxonomy or transition matrix exists.** Behavior that resembles "label transitions" is emergent from flow-stack + priority + coercions + accumulator, not an explicit layer.

---
## 2. Repository Map

Workspace root `Main/` is **not** a git repo; it contains two sibling repos:

| Component | Path | Language | Runtime entry |
|-----------|------|----------|---------------|
| Brain (dialogue) | `Collection/` | Python 3.13 / FastAPI | `Collection/app/main.py` — HTTP `POST /turn`, WS `/ws/brain` |
| Media server | `Websocket/` | Go | `Websocket/cmd/server/main.go` — WS `/stream` (carrier) |
| Workers | `Websocket/workers/{denoise,amd,semantic_turn}` | Python | length-prefixed TCP/UDS microservices |

**Inter-service communication:** Carrier ⇄ Go over WebSocket (audio); Go ⇄ brain over WebSocket `ws://brain:8000/ws/brain` (JSON turn contract); brain ⇄ external over HTTPS (LLM, KB, WhatsApp, transfer).

| Concern | Location |
|---------|----------|
| Config | `Collection/app/config.py` (pydantic-settings); server `.env` generated by `deploy_test_server.py`; Go env via `*ConfigFromEnv()` |
| Flows / templates | `Collection/app/flows/*.yml`, `Collection/app/flows/salary_on_time/{pre_closure,on_due,post_due}.yml`; reply manifest `Collection/app/flows/reply_manifest.json` |
| Memory / state | `Collection/app/memory/{store,composite,postgres_borrowers,upstash,audit}.py`; schemas `Collection/app/schemas/state.py` |
| Model / LLM / KB | `Collection/app/clients/{llm_vertex,llm_groq,kb,whatsapp,transfer,tools_sim,sot_tools_sim}.py` |
| Tests | `Collection/tests/{golden,unit,compliance,sim,fixtures,helpers}` |
| Telephony / session | `Websocket/internal/media/*`, `Websocket/internal/brain/*` |
| Deploy | `Websocket/deploy/docker-compose*.yml`; `Main/deploy_test_server.py` |

---

## 3. Current End-to-End Pipeline

```mermaid
sequenceDiagram
    participant C as Carrier (audio)
    participant Go as Go media server
    participant ST as semantic_turn worker
    participant Br as Brain /ws/brain
    participant HT as handle_turn
    participant GT as compliance gate
    participant TTS as TTS (11labs/Sarvam)

    C->>Go: PCM frames (session_start, media)
    Go->>Go: transcode → denoise → AMD gate → Sarvam ASR
    Go->>ST: Predict(transcript, audio) [EOU]
    ST-->>Go: {complete, confidence}
    Go->>Br: {type:turn, transcript, flow_class}
    Br->>HT: handle_turn(TurnRequest, on_gated_reply)
    HT->>HT: load→guard→safety→retrieval→LLM→coercions→floor→accumulator→executor
    HT->>GT: process_outbound_reply(draft)
    GT-->>HT: final reply_text (allow/block/modify)
    HT-->>Br: on_gated_reply → chunk(s); then FlowClass + Done{end_call,disposition}
    Br-->>Go: chunk / flow_class / done
    Go->>TTS: Speak(chunk text)
    TTS-->>Go: PCM audio
    Go->>C: audio egress; hangup if end_call
```

**Stage → routing/state/text impact (high level):**

- Go media chain: **no routing** except `flow_class` (endpointing silence thresholds only).
- `handle_turn` retrieval + LLM + coercions + floor + accumulator: **routing**.
- `tracker.apply` + `priority.reorder` + `executor`: **routing + state + text** (`reply_id`).
- `nlg.draft_reply_resolved`: **final draft text**.
- `gate.gate`: **final text** (may block/modify), can set transfer.
- `_persist_turn`: **state**.

---
## 4. File-by-File Pipeline Findings

| Stage | File | Function/Class (line) | Input | Output | Routing impact | State impact |
|-------|------|-----------------------|-------|--------|----------------|-------------|
| WS route | `app/main.py` | `brain_ws` (L118) | WS connection | delegates | No | No |
| HTTP route | `app/main.py` | `turn` (L102) | `TurnRequest` | `TurnResponse` | No | No |
| WS handler | `app/ws/handler.py` | `handle_brain_websocket` (L186) | Go JSON msgs | session mgmt | No | Session |
| Turn runner | `app/ws/handler.py` | `_run_turn` (L71) | `turn` msg | chunks + done | No | No |
| Barge-in/supersede | `app/ws/session.py` | `supersede_and_run` (L54) | new turn_id | cancels stale | **Yes (latest wins)** | No |
| **Orchestrator** | `app/engine/turn.py` | **`handle_turn` (L1005)** | `TurnRequest` | `TurnResponse` | **Yes** | **Yes** |
| Load/hydrate | `app/engine/tracker.py` | `hydrate_from_borrower` (L23) | borrower | slots | Force-flow inject | Slots |
| Terminal guard | `app/engine/turn.py` | `_run_closed_early_exit` (L933) | closed slots | empty reply, end | **Early exit** | attempts |
| Safety pre-empt | `app/engine/safety.py` | `safety_preempt` (L9) | transcript+emotion | care reply | **Early exit** | compliance flags |
| Emotion | `app/engines_p2/emotion.py` | `classify_emotion_from_turn` (L223), `apply_emotion_to_state` (L278) | transcript/prosody | emotion slots | Indirect | Slots + event |
| Frustration | `app/engine/robustness.py` | `track_frustration` (L107) | emotion slots | counter/escalate | Escalation | `_frustration_turns` |
| On-rails detect | `app/engine/turn.py` | `_awaiting_collect_slot` (L151) | flow_stack | awaiting slot | Gates retrieval | No |
| Retrieval | `app/engine/retrieval.py` | `retrieve_flow_candidates` (L124) | transcript,tenant | `FlowCandidate[]` | **Yes** | No |
| LLM commands | `app/engine/command_gen.py` | `generate` (L530) | transcript,candidates | commands | **Yes** | No |
| SOT coercions | `app/engine/turn.py` | `_coerce_sot_*` (L170–494) | transcript,slot | rewritten cmds | **Deterministic override** | via set_slot |
| Slot validation | `app/engine/slot_validation.py` | `validate_commands` (L98) | commands | filtered | Drops bad sets | No |
| Clarify (ambiguous) | `app/engine/turn.py` | `_clarify_if_ambiguous` (L497) | top-2 scores | maybe clarify | Yes (SOT off) | No |
| Confidence floor | `app/engine/turn.py` | `_suppress_low_confidence_flow_jumps` (L557) | scores,pinned | drop start_flow | **Yes** | No |
| Dispute accumulator | `app/engine/turn.py` | `_accumulate_dispute_evidence` (L621) | cross-turn counts | force start_flow | **Yes** | `_dispute_evidence` |
| Apply commands | `app/engine/tracker.py` | `apply` / `_apply_command` (L64/78) | commands | mutated state | **flow_stack push/pop** | **Yes** |
| Priority reorder | `app/engine/priority.py` | `reorder` (L36) | flow_stack | parked frames | **Active flow** | flow_stack order |
| Decision overlay | `app/engines_p2/decision_overlay.py` | `apply_decision_overlay` (L341) | trust/risk/persona | overlay slots | Indirect | Slots + event |
| Executor | `app/engine/executor.py` | `run_async` (L168) | state | `ExecResult` | Walks flow | step_index, actions |
| Transfer hook | `app/engine/turn.py` | live bridge (L1350) | transfer slots | transfer status | No | disposition |
| WhatsApp hook | `app/engine/turn.py` | `_send_whatsapp_bg` (L1393) | whatsapp slots | send | No | `whatsapp_sent` |
| Repair track | `app/engine/robustness.py` | `track_slot_reask` (L63) | question_slot | escalate | Escalation | `_repair_counts` |
| NLG | `app/engine/nlg.py` | `draft_reply_resolved` (L383) | reply_id/slots | draft text | No | context slots |
| Compliance gate | `app/engine/gate.py` | `gate` (L18) | draft,state | final text | Transfer flag | No |
| Decision log | `app/engine/turn_decision_log.py` | `log_turn_decision` (L86) | turn context | JSON log | No | No |
| Flow-exhaustion guard | `app/engine/turn.py` | (L1520) | empty stack (SOT) | end_call | Force hangup | Yes |
| Persist | `app/engine/turn.py` | `_persist_turn` (L806) | state,audit | audit_id | No | DB + audit |

**Go media stages (no dialogue routing unless noted):**

| Stage | File | Function/Type | Routing impact |
|-------|------|---------------|----------------|
| Session create | `internal/media/session.go` | `SessionManager.Create` (L135) | No |
| Bootstrap brain+TTS | `internal/brain/bootstrap_sink.go` | `OnStart` (L24) | No |
| ASR | `internal/media/sarvam_asr.go` | `parseSarvamMessages` (L584) | No |
| EOU / barge-in | `internal/media/turn_manager.go` | `tryEmitEndOfTurn` (L442), `OnSpeechStart` (L208) | `flow_class` endpointing only |
| Brain client | `internal/brain/client.go` | `OnTurnEvent` (L171), `dispatchInbound` (L328) | Sends transcript, receives chunk/done |
| TTS | `internal/media/tts_reply_consumer.go` | `OnReplyChunk` (L179), `OnReplyDone` (L200) | No |

---
## 5. Routing and Decision Inventory

| # | Mechanism | Status | File / Function (line) | Trigger | Effect on routing / reply |
|---|-----------|--------|------------------------|---------|---------------------------|
| 1 | KB dense retrieval | Exists | `retrieval.py:retrieve_flow_candidates` (L124); `kb.py` | Every non-on-rails SOT turn | Produces `FlowCandidate{name,description,score}` → LLM candidate set |
| 2 | Retrieval cache | Exists | `retrieval.py` (L25–56) | Repeat transcript/tenant | TTL 300s, 128 entries; SHA256 key |
| 3 | Reranking | Not found | — | — | Per-flow max-score dedupe only (`resolve_flows` L117) |
| 4 | LLM command generation | Exists | `command_gen.py:generate` (L530) | Every turn (unless skipped) | Emits structured commands |
| 5 | JSON schema restriction | Exists | `command_gen.py:build_response_schema` (L486) | LLM call | Flow enum = candidates; slot enum = active collect |
| 6 | Command validation / rejection | Exists | `command_gen.py:parse_and_validate_commands` (L401) | LLM output | Drops unknown flow/slot/blocked cmd; malformed → `clarify` |
| 7 | Blocked commands (SOT) | Exists | `turn.py` (L148); `command_gen` (L104) | SOT tenant | Removes `human_handoff`/`cannot_handle` |
| 8 | Deterministic dispute matcher | Exists | `turn.py:_sot_dispute_flow` (L319), `_coerce_sot_dispute` (L377) | On-rails + hard cues | Force `start_flow` → `sot_obj_*` |
| 9 | Push-willing coercion | Exists | `turn.py:_coerce_sot_push_willing` (L433) | Awaiting push slot + affirmative | `set_slot`=`willing` |
| 10 | Identity yes/no coercion | Exists | `turn.py:_coerce_sot_identity` (L223) | Awaiting `sot_identity_response` | `confirmed`/`denied` |
| 11 | Commit-reversal coercion | Exists | `turn.py:_coerce_sot_commit_reversal` (L469) | Awaiting reversal slot + refusal | `start_flow` → `sot_obj_no_timeline` |
| 12 | Final-confirm coercion | Exists | `turn.py:_coerce_sot_confirm` (L170) | Awaiting `sot_final_confirm` | yes/no |
| 13 | Link-received coercion | Exists | `turn.py:_coerce_sot_link_received` (L268) | Awaiting `sot_link_received` | `received`/`not_received` |
| 14 | Confidence floor (Layer 3) | Exists | `turn.py:_suppress_low_confidence_flow_jumps` (L557); `SOT_FLOW_FLOOR=0.6` | Digression + awaiting slot | Drop `start_flow` if score < floor → `clarify` |
| 15 | Pinned flows (Layer 0) | Exists | `turn.py:_merge_pinned_flow_candidates` (L529); `SOT_PINNED_FLOWS` | Digression on | Inject critical flows (score=None), exempt from floor |
| 16 | Dispute evidence accumulator | Exists | `turn.py:_accumulate_dispute_evidence` (L621); `SOT_DISPUTE_BAR=2` | Cross-turn dispute evidence | Force `start_flow` at bar |
| 17 | Priority ladder reorder | Exists | `priority.py:reorder` (L36) | Every command batch | Highest-priority frame active; others parked |
| 18 | Flow stack manipulation | Exists | `tracker.py:_apply_command` (L78) | `start_flow`/`cancel_flow` | Push / remove frames |
| 19 | Active collect slot handling | Exists | `turn.py:_awaiting_collect_slot` (L151); `executor.py` (L205) | Top frame collect step | Pause turn / gate retrieval |
| 20 | Ambiguity clarification (F6) | Exists (SOT off) | `turn.py:_clarify_if_ambiguous` (L497) | Top-2 scores within delta | Replace start_flow with `clarify` |
| 21 | Repair / reask guard | Exists | `robustness.py:track_slot_reask` (L63) | Same slot re-asked | Escalate at `max_slot_retries=2` |
| 22 | Frustration guard | Exists | `robustness.py:track_frustration` (L107) | med/high anger streak | Escalate at `SOT_FRUSTRATION_TURNS=3` |
| 23 | Human handoff / transfer | Exists | `actions.py:transfer_call` (L808); `turn.py` (L1350) | Dispute/refusal flows | Set transfer + end_call; live POST |
| 24 | Compliance gate | Exists (SOT tenant off) | `gate.py:gate` (L18) | Outbound reply | Block/modify; call window, opt-out, debt disclosure |
| 25 | Safety pre-empt | Exists | `safety.py:safety_preempt` (L9) | Inbound distress | Early exit, care reply, maybe transfer |
| 26 | Identity entry gate | Exists | `identity_gate.py:apply_identity_entry_gate` (L110) | No `identity_ok` | Insert `identity_verification` at stack[0] |
| 27 | Terminal guard | Exists | `turn.py` (L1113), `_run_closed_early_exit` (L933) | `end_call`/`sot_call_closed` | Skip LLM, force disconnect |
| 28 | Flow-exhaustion guard | Exists | `turn.py` (L1520) | Empty stack (SOT) | Close call |
| 29 | Tenant/scenario flags | Exists | `config.py:tenant_config` (L287) | Tenant = `salary_on_time` | Disables compliance gate, sets caps, digression |
| 30 | Language logic | Partial | `nlg.py:normalize_language` (L120); Go `ResolveSessionASRLanguage` | Borrower language | Variant selection; ASR locale |
| 31 | ASR EOU / semantic turn | Exists (Go) | `turn_manager.go` (L442); `semantic_turn` worker | Silence + model | Turn boundary; endpointing |
| 32 | Barge-in | Exists (Go) | `bargein.go:commit` (L278) | Speech during agent | Cancel TTS + brain turn |
| 33 | Turn superseding | Exists | `ws/session.py:supersede_and_run` (L54); Go `client.go` (L386) | New transcript mid-turn | Cancel/merge stale turn |
| 34 | Fallback / dead-air | Exists (Go) | `watchdog.go` (`FALLBACK_NO_AUDIO_MS`) | No audio | Speak holding line |
| 35 | Error fail-soft (KB/LLM) | Exists | `kb.py`,`llm_*.py` | External error | Return `[]` / `clarify` (never crash turn) |
| 36 | Analytics / audit | Exists | `audit.py`; `turn_decision_log.py` (L86); `telemetry.py` | Every turn | Persisted audit chain + OTEL spans + decision log |
| 37 | Decision overlay (P2) | Exists | `decision_overlay.py` (L341) | Trust/risk/persona | Overlay slots influencing tone |
| 38 | Force test flow | Exists | `turn.py` (L1099) | `TEST_MODE`/force_flow meta | Inject flow into stack |

---
## 6. Current State and Memory

### 6.1 Conversation state schema — `app/schemas/state.py`

```19:27:c:\Users\nitis\source\repos\Main\Collection\app\schemas\state.py
class ConversationState(BaseModel):
    call_id: str
    tenant_id: str
    borrower_id: str
    slots: dict[str, Any] = Field(default_factory=dict)
    flow_stack: list[Frame] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    attempts: int = 0
    version: int = 0
```

`Frame` (L6–9): `flow: str`, `step_index: int = 0`, `parked: bool = False`.
`Event` (L12–16): `ts`, `kind`, `data`, `rationale`.

- **`recent_turns` is not a field** — derived at runtime from `events` (`command_gen.py` L122–129; `persona.py` L173–182).
- Emotion/risk/trust/persona are **hydrated into `slots`** at turn start; durable history lives on `BorrowerRecord`.
- `disposition` lives in `slots` during the call and is returned in `TurnResponse`.

### 6.2 Borrower record — `app/schemas/state.py` L30–50

`BorrowerRecord` carries: `identity`, `loan`, `payments`, `ptps`, `broken_ptps`, `excuses`, `emotions`, `hardships`, `disputes`, `trust_current` (+`trust_history`), `risk_flags`, `persona_current` (+`persona_history`), `recovery`, `comms_prefs`, `payment_links`, `compliance_flags`, `notes`.

### 6.3 Memory stores — `app/memory/`

| Store | File | Backend | TTL / versioning |
|-------|------|---------|------------------|
| `InMemoryMemoryStore` | `store.py` L29–79 | process dicts | optimistic version lock |
| `UpstashMemoryStore` | `store.py` L82–158 | Redis REST | state TTL ~4h (`state_ttl_seconds` L197); borrower/audit no TTL |
| `CompositeMemoryStore` | `composite.py` L15–86 | Postgres borrowers + inner state/audit | PG upsert then inner fallback |
| `PostgresBorrowerStore` | `postgres_borrowers.py` | Postgres `borrowers` | phone suffix lookup L108 |
| Audit | `audit.py` | Upstash list / in-memory | append-only, `export_schema_version:3` |

Concurrency: `save_state` expects `version == existing+1` else `StaleStateError`; version bumped in `tracker.apply` (L74).

### 6.4 Special slot keys

- Internal counters: `_dispute_evidence`, `_frustration_turns`, `_repair_counts`, `_force_test_flow`, `_forced_flow_injected`, `_skip_flow_pop`.
- Call control: `end_call`, `sot_call_closed`, `disposition`, `disposition_logged`.
- Transfer: `transfer_to_human`, `transfer_requested`, `transfer_initiated`, `transfer_status`, `transfer_reason`, `transfer_target`.
- WhatsApp/link: `whatsapp_requested`, `whatsapp_sent`, `whatsapp_phone`, `whatsapp_name`, `payment_link_sent`.
- Read-only (hidden from LLM): `command_gen.READ_ONLY_LLM_SLOTS` (L31–59) + any `_`-prefixed key.

### 6.5 Per-turn engines (`app/engines_p2/`)

| Engine | Hot path? | Reads | Writes |
|--------|-----------|-------|--------|
| Emotion (`emotion.py`) | Yes (L1080) | transcript, prosody | `emotion`, `emotion_intensity`, `tone_register` slots + event; borrower.emotions on persist |
| Risk (`risk.py`) | Hydrate only | borrower history | `risk_flags` slot; **decay** `merge_with_decay` L567 (factor 0.55) |
| Trust (`trust.py`) | Hydrate only | ptps/payments/excuses | `trust` slot; **recency decay** L83 |
| Persona (`persona.py`) | Hydrate + overlay | trust/risk/history | `persona` slot; `persona_history` transitions (from/to) L462 |
| Decision overlay (`decision_overlay.py`) | Yes (L1341) | trust/risk/persona | overlay slots + event |

### 6.6 Capability assessment (does current state support…?)

| Capability | Verdict | Evidence |
|------------|---------|----------|
| `active_label` | **No** | No such field; nearest are `persona_current`, `emotion` slot, `risk_flags` (disjoint) |
| `previous_label` | **Partial** | `persona_history.from/to` (L462); prior emotion recoverable from events |
| `unresolved_high_risk_label` | **No** | `SERIOUS_LABELS` for detection (`risk.py` L23) but no persisted unresolved-label field |
| Resolved labels | **Partial** | Risk flags decay/drop below `MIN_CONFIDENCE=0.35`; no explicit `resolved_at` |
| Evidence accumulation | **Yes (scoped)** | `_dispute_evidence` (L621); risk flags carry `evidence[]` |
| Evidence decay | **Partial** | Risk/trust decay only; `_dispute_evidence`/`_frustration_turns` have **none** |
| Label transition tracking | **Partial** | Persona transitions only (`persona_history`); no unified log |
| Conversion analytics (`refusal_then_paid`) | **No** | No field/event/query; raw material exists in payments/notes/audit |

---
## 7. Label / Intent / Transition Investigation

**Verdict: #3 — "Only basic flow switching exists, but no label transition layer"**, with strong **#2 elements** (similar behavior indirectly through flow/tracker/coercion/priority/accumulator logic).

Evidence for the verdict:

- **No label taxonomy / intent taxonomy** object exists. There is no enum of intents, no classifier that outputs an "intent label", no `active_label`/`current_label`/`previous_label` field, and **no transition matrix** anywhere in `Collection/app`.
- The nearest thing to "intent" is the **flow name** on the `flow_stack` and the **priority category** (`PriorityType`: opt_out/vulnerable/identity/dispute/hardship/ptp/refusal/reminder in `schemas/flow.py` L49–58). This is a fixed priority ladder, not a learned/tracked transition.
- **Indirect equivalents that solve parts of the "label transition" problem** (documented so they are not missed):
  - **Priority reorder** (`priority.py:reorder` L36): when a higher-priority flow (e.g. dispute) is pushed while a lower one (offer) is active, the higher one becomes active and the lower is parked — an implicit "current intent overrides previous intent" behavior.
  - **Deterministic coercions** (`turn.py` L170–494): map utterances to high-stakes routes regardless of LLM/KB, functioning like "label overrides".
  - **Cross-turn dispute evidence accumulator** (`turn.py:_accumulate_dispute_evidence` L621): the only explicit "repeated signal → force route" (confidence accumulation) mechanism; per-theme counts in `slots["_dispute_evidence"]`, forced route at `SOT_DISPUTE_BAR`.
  - **Frustration guard** (`robustness.py:track_frustration` L107): repeated-signal handling for anger/frustration → escalation.
  - **Persona transitions** (`persona.py` L462): the only place a "previous vs current" transition is explicitly recorded (`persona_history` with `from`/`to`).
- **High-risk label persistence: partial.** Risk flags persist on `BorrowerRecord.risk_flags` with confidence + decay, but there is no per-call "unresolved high-risk label that blocks closing until resolved" concept. A dispute flow typically routes to transfer immediately rather than persisting an unresolved-label state.
- **Evidence buckets: yes, but only for disputes.** No general-purpose evidence bucket per label.

Conclusion: routing "transitions" are emergent from `flow_stack + priority + coercions + accumulator`, not an explicit, inspectable label-transition layer.

---

## 8. Flow Switching and Flow Lifecycle

Flows are declarative YAML (`Flow{description, priority, steps[]}`; `FlowStep{id, utter, collect, action, decide, next, end}` — `schemas/flow.py` L14–40), loaded and merged by `flows/loader.py:load_all_flows` (L30–45; later files override name collisions). Descriptions feed KB (`retrieval.tagged_flow_text` L81) and the LLM candidate list; `flow_doc_map.json` is currently `{}`.

| Feature | Status | File / Function (line) | Notes |
|---------|--------|------------------------|-------|
| Start new flow | Exists | `tracker.py:_apply_command` (L82) | Appends `Frame`; no dedupe |
| Cancel flow | Exists | `tracker.py:_apply_command` (L88) | By name (all matching) or pop top |
| Replace active flow | Partial | `actions.py` (L646, L702, L746) | Via `sot_chain_*`/`route_to_*` actions + `_skip_flow_pop`; **no LLM `replace_flow` command** |
| Stack flow (digression) | Exists | `tracker.py` (L82) + `priority.py:reorder` (L36) | Append + park parent |
| Reorder by priority | Exists | `priority.py:reorder` (L36); ladder L7–16 | Highest active, rest parked |
| Suppress low-confidence jump | Exists | `turn.py:_suppress_low_confidence_flow_jumps` (L557) | Needs digression + awaiting slot |
| Clarify ambiguous jump | Exists (SOT off) | `turn.py:_clarify_if_ambiguous` (L497) | `clarify_on_ambiguous_flow=false` for SOT |
| Deterministic override | Exists | `turn.py` L170–494; `_sot_dispute_flow` L319 | 6 coercions |
| Pinned high-risk flow | Exists | `turn.py:_merge_pinned_flow_candidates` (L529) | Layer 0, digression on |
| Human transfer | Exists | `actions.py:transfer_call` (L808); `turn.py` (L1350) | Stub/live via `TRANSFER_MODE` |
| Terminal guard | Exists | `turn.py` (L1113, L1520); `executor.py` (L229) | `end_call`/`sot_call_closed`/empty stack |
| Repair/reask guard | Exists | `robustness.py:track_slot_reask` (L63) | Cap `max_slot_retries=2` |
| Slot validation | Exists | `slot_validation.py:validate_commands` (L98) | FACT_SLOTS + type validators; enums route via `else` not hard-reject |
| Active slot detection | Exists | `turn.py:_awaiting_collect_slot` (L151) | Drives on-rails + floor |
| Flow completion | Exists | `executor.py` `END_TARGET` (L8), `_goto_target` (L57), `step.end` (L250) | Pop frame; chain uses `_skip_flow_pop` |
| Side effect: WhatsApp / link | Exists | `actions.py:send_whatsapp_message` (L774); `turn.py` (L1393) | Sets `whatsapp_sent=False` to allow re-send |
| Side effect: transfer | Exists | `actions.py` (L808) | Sets terminal + disposition |
| Side effect: hangup | Exists | `actions.py:hangup_call` (L823) | `end_call`+`sot_call_closed` |
| Digression enable/disable | Exists | `SOT_DIGRESSION` (`config.py` L125); on-rails suppress (`turn.py` L1212) | Default off; objection suppression conditional |
| Resume parked parent | Partial | `priority.py` (L42); `actions.py` (L719) | Auto-unpark when sole frame |

**Extra lifecycle details:** `sot_chain_*` actions *replace* the top frame instead of stacking; `MAX_EXECUTOR_STEPS=256` guards runaway walks; `decide` uses `conditions.evaluate_condition` (`==`,`!=`,`<`,`<=`,`>`,`>=`,`in`), not raw `==`.

---
## 9. Model and External Service Inventory

| Service | Provider | Where called | Input / Output | Timeout / Retry / Fallback |
|---------|----------|--------------|----------------|----------------------------|
| ASR | Sarvam (Go) | `Websocket/internal/media/sarvam_asr.go` | audio → transcript events | reconnect; no Python client |
| TTS | ElevenLabs (default) / Sarvam | `Websocket/internal/media/tts*.go` (`TTS_PROVIDER`) | text → PCM | streaming; cache |
| LLM | Vertex/Gemini (`gemini-2.5-flash`) or Groq | `command_gen.py:generate` (L530) → `clients/llm_vertex.py` / `llm_groq.py` | system+user prompt → JSON commands | Vertex 30s + 1 retry + schema-drop fallback; Groq 20s + 2 attempts; stub → `"[]"` |
| KB / vector search | `api.fonada.ai/search` | `retrieval.py` → `clients/kb.py` | query,top_k → `[{doc_id,score,text}]` | 10s, single attempt/URL, 404→legacy path, error→`[]` (fail-soft) |
| Reranker/classifier | **Not found** | — | — | per-flow max-score dedupe only |
| Semantic turn detector | Smart Turn v3 ONNX (Go worker) | `Websocket/workers/semantic_turn/server.py` | transcript+audio → `{complete,confidence}` | timeout `SEMANTIC_TURN_TIMEOUT_MS` (~100ms), silence fallback |
| Emotion/risk/trust/persona | Rule-based (Python) | `app/engines_p2/*` | state/history → slots | in-process; no external call |
| WhatsApp / payment link | `app.fonada.ai/.../whatsapp_campaign_creator` | `actions.py` (L774) → `clients/whatsapp.py` | phone,name → status | 10s; stub `pending`; error `failed` (never raises) |
| Transfer | `voip.ivrobd.com` | `turn.py` (L1350) → `clients/transfer.py` | call_id,target,reason → status | 10s; `transfer_hold_ms=4500` before live POST; stub/never raises |
| Tool simulators | in-memory | `clients/tools_sim.py`, `sot_tools_sim.py` (`TOOLS_MODE`) | tool,args → result | idempotency cache; error injection |
| Memory | Postgres + Upstash Redis / in-memory | `app/memory/*` | state/borrower/audit | state TTL ~4h; version lock |
| Startup validation | — | `app/startup_validation.py`; `main.py` L30 | settings → exit(1) on invalid live config | validates LLM/KB creds when not stub |
| Telemetry | OpenTelemetry (optional) | `app/telemetry.py` | spans | graceful no-op if not installed |

**Key config/env flags** (`config.py`): `LLM_PROVIDER`, `LLM_STUB`, `GEMINI_MODEL_ID`, `GROQ_MODEL`, `KB_STUB`, `KB_TOP_K=10`, `KB_SEARCH_PATH`, `WHATSAPP_MODE`, `TRANSFER_MODE`, `SOT_DIGRESSION`, `SOT_PINNED_FLOWS`, `SOT_FLOW_FLOOR=0.6`, `SOT_DISPUTE_FLOWS`, `SOT_DISPUTE_BAR=2`, `SOT_FRUSTRATION_TURNS=3`, `TEST_MODE`, `STUB_MODE`, `CALL_WINDOW_START/END/TIMEZONE`, `MAX_ATTEMPTS_PER_DAY`. Go: `BRAIN_WS_URL`, `TTS_PROVIDER`, `SARVAM_*`, `SEMANTIC_TURN_*`, `FALLBACK_NO_AUDIO_MS`, `BARGEIN_ENABLED`, `AMD_ENABLED`.

---

## 10. Existing Tests and Coverage

Structure: `tests/golden/` (behavior lock), `tests/unit/` (module), `tests/compliance/` (release gate), `tests/sim/*.json` (scripted scenarios), `tests/fixtures`, `tests/helpers`. CI stubs forced in `tests/conftest.py` (all providers stub, digression off, window 08:00–19:00 IST). Scripted `ScriptedKB`/`ScriptedLLM` in `app/sim/scripted_clients.py`.

**Scenario coverage matrix:**

| Scenario | Verdict | Evidence (file:test) |
|----------|---------|----------------------|
| Refusal then payment link | **Not found** | refusal ladders + link exist separately, not chained |
| Soft refusal then promise-to-pay | Partial | `test_on_post_due.py:test_on_due_push_ladder_then_commit` (L104); `dynamic_ptp.json` |
| Dispute then payment request | Partial | `test_robustness_fs5.py:test_cross_flow_dispute_during_partial_payment` (L322); `test_adversarial_e2e.py` (L85) |
| Wrong person then payment request | Partial | `test_repair_layer.py:test_identity_wrong_number_denies` (L266); no denial→link chain |
| Repeated dispute escalation (accumulator) | Partial | `test_repair_layer.py:test_accumulator_forces_route_on_second_weak_turn` (L377) — function-level, not full turn |
| Low-confidence suppression | **Exists** | `test_sot_pre_closure.py:test_layer3_suppresses_weak_flow_jump` (L557) |
| Ambiguous clarification | **Exists** | `test_slot_validation.py:test_ambiguous_tie_becomes_clarify` (L101) |
| Active flow continuation / digression resume | **Exists** | `test_sot_pre_closure.py:test_digression_on_rails_retrieves_and_resumes` (L401) |
| Flow cancellation | **Exists** | `test_sot_pre_closure.py:test_cancel_flow_empties_stack_disconnects` (L346) |
| Pinned flows (Layer 0) | **Exists** | `test_sot_pre_closure.py:test_layer0_pins_link_request_when_kb_misses` (L532) |
| Confidence floor (Layer 3) | **Exists** | `test_sot_pre_closure.py:test_layer3_*` (L557, L584, L605) |
| Human handoff / transfer | **Exists** | `test_sot_pre_closure.py:test_objection_never_loan_transfers_simulated` (L180); `test_transfer.py` |
| Compliance / release gate (call window) | **Exists** | `test_release_gate.py:test_out_of_window_silent` (L69); `after_hours.json` |
| Frustration escalation | Partial | `test_repair_layer.py:test_frustration_escalates_after_threshold` (L428) — helper only |
| Payment link send + receipt + hangup | **Exists** | `test_sot_pre_closure.py:test_link_request_confirms_receipt_then_hangs_up` (L466), `..._not_received_resends...` (L492) |
| State / memory persistence | **Exists** | `test_memory_tracker.py` (L16–66); `test_api.py:test_turn_increments_state_on_second_call` (L50) |

**Gap:** e2e multi-turn transitions through the full `handle_turn` pipeline (refusal→link, wrong-person→link, accumulator, frustration-through-emotion-engine) are under-covered; most such logic is tested at the helper/function level.

---
## 11. Example Trace A: Refusal Then Payment Link

Starting condition: bot is on-rails asking for a payment commitment (e.g. awaiting `sot_customer_time`/a push slot; active flow `sot_push`/`sot_commit`).

**Turn 1 — "I will not pay":**
1. `handle_turn` runs; on-rails is detected (`_awaiting_collect_slot` L151, `SOT_ONRAILS_FLOWS` L135). With digression **off** (default), retrieval is skipped (`turn.py` L1185) and `sot_obj_*` candidates are stripped.
2. LLM emits a `set_slot` for the awaited slot or a refusal; `_coerce_sot_push_willing` (L433) will *not* fire (no affirmative cue); `_coerce_sot_commit_reversal` (L469) *may* fire if awaiting a reversal slot with refusal cues → `start_flow: sot_obj_no_timeline`.
3. Reply is typically the push/reason prompt ("Kya issue aa raha hai payment mein?"). The active flow stays on the stack.
4. **Old refusal "state":** there is no persisted refusal label; it exists only as the slot/flow position and the recorded `Event`.

**Turn 2 — "Ok, I will pay. Send me the payment link.":**
1. If still on-rails with digression off, `sot_obj_link_request` is **suppressed from candidates** unless it is pinned. It **is** in the default `SOT_PINNED_FLOWS`, but pins are only merged when `sot_digression` is on (`turn.py` L1228). **Therefore, with digression OFF, a mid-rails jump to the link flow depends on the LLM setting the awaited slot / the coercions, and may not switch cleanly** — this matches the historical bug.
2. With digression **ON**: retrieval runs, `sot_obj_link_request` is a candidate (via KB and/or pinned merge L529), exempt from the confidence floor (score=None). The LLM emits `start_flow: sot_obj_link_request`; `tracker.apply` pushes it; `priority.reorder` makes it active (or parks the offer). Executor runs the link flow: `utter sot_obj_link_request` → `send_whatsapp_message` → `collect sot_link_received` → `decide` → hangup.

**Answers:**
- Can it switch refusal→link? **Yes when digression is on** (or via pinned flow); **fragile when digression is off** (default).
- Depends on KB retrieval? Yes (candidate) unless pinned. Depends on LLM command-gen? Yes (emits `start_flow`).
- Confidence floor suppression? Not for `sot_obj_link_request` (pinned, exempt). Would apply to non-pinned weak jumps.
- Old flow on stack? Yes — parked, and resumes if the link flow pops without closing (but the current link flow ends the call).
- Old refusal state resolved? Not explicitly resolved; overwritten by active flow change; the refusal `Event` remains in history.
- Final reply template: `sot_obj_link_request` reply, then `sot_link_thanks_close`/`sot_link_retry_wait` (resolvable from `pre_closure.yml` + `reply_manifest.json`). **Final TTS text is knowable from code** for the deterministic branches.

## 12. Example Trace B: Loan Dispute Then Payment Link

Starting condition: bot is asking for payment (on-rails).

**Turn 1 — "Maine loan liya hi nahi":**
1. On-rails detected. `_coerce_sot_dispute` (L377) calls `_sot_dispute_flow` (L319), which matches the never-loan cue → returns `sot_obj_never_loan`. Because on-rails, it **replaces** the LLM commands with `start_flow: sot_obj_never_loan` (deterministic override — does not depend on KB/LLM).
2. In parallel, `_dispute_evidence_this_turn` (L598) records evidence; `_accumulate_dispute_evidence` (L621) increments the theme (but the deterministic route already fires this turn).
3. `sot_obj_never_loan` has `priority: dispute` and its steps route to `transfer_call` → `end_call=True` + `sot_call_closed=True` (`pre_closure.yml` L519–527; `actions.py` L808).

**Turn 2 — "Theek hai link bhej do":**
1. **If Turn 1 transferred/closed the call**, the turn-start terminal guard (`turn.py` L1113 → `_run_closed_early_exit` L933) short-circuits: empty reply, `end_call` re-issued. The link request **cannot** override a completed dispute→transfer. This is by design (dispute is high-risk).
2. **If the dispute did NOT deterministically fire** (e.g. softer phrasing that misses the matcher and is below the confidence floor), then the accumulator may not have reached the bar (`SOT_DISPUTE_BAR=2`), the call stays open, and Turn 2's "link bhej do" can route to `sot_obj_link_request` (pinned/candidate under digression). In that path the system sends the link rather than resolving the dispute.

**Answers:**
- Deterministic dispute handling? **Yes** (`_sot_dispute_flow` matcher).
- Pinned/coerced? Coerced on-rails; dispute flows also appear in `SOT_PINNED_FLOWS`/`SOT_DISPUTE_FLOWS`.
- Evidence accumulated across turns? **Yes** (`_dispute_evidence`), but deterministic match usually fires first.
- High-risk state persists? Only as the transferred/closed call state; **no persistent "unresolved dispute label"** that would block a future link in the same open call — resolution is via transfer/close.
- Can link override dispute? **No** once dispute routed to transfer/close (terminal guard). **Yes** if the dispute was missed and call stayed open — a real risk.
- Where controlled? `turn.py` coercions (L377), accumulator (L621), terminal guard (L1113); `pre_closure.yml` dispute flows.
- Unclear: exact matcher coverage for paraphrased denials (whether soft denials fall through to the accumulator vs. immediate route) needs runtime verification against real transcripts.

---
## 13. Extra Routing-Relevant Features Discovered

| Feature | File / Function | What it does | Why it matters |
|---------|-----------------|--------------|----------------|
| Emotion engine | `engines_p2/emotion.py` (L223) | Classifies emotion/intensity/tone each turn | Feeds frustration guard, decision overlay, NLG tone |
| Risk engine w/ decay | `engines_p2/risk.py` (L511, L567) | Computes risk flags from history with confidence decay | Nearest to "unresolved risk", drives overlay |
| Trust engine w/ recency | `engines_p2/trust.py` (L83) | Recency-weighted trust score | Personalization, overlay |
| Persona engine + transitions | `engines_p2/persona.py` (L447, L462) | Classifies persona; logs from/to transitions | The one true "label transition" record present |
| Decision overlay | `engines_p2/decision_overlay.py` (L341) | Applies trust/risk/persona to per-turn decisions | Shapes tone/reply beyond flow |
| Identity entry gate | `identity_gate.py` (L110) | Forces identity verification before collection | Compliance/safety |
| Safety pre-empt | `safety.py` (L9) | Inbound distress/vulnerability short-circuit | Customer safety, RBI compliance |
| Compliance gate | `gate.py` (L18) | Outbound blocking (window/opt-out/disclosure/caps/phrases) | Legal compliance |
| Repair layer | `robustness.py` (L34, L63) | Re-ask tracking + escalation cap | Prevents infinite loops |
| Frustration guard | `robustness.py` (L107) | Streak-based escalation | UX + de-escalation |
| Turn superseding / barge-in | `ws/session.py` (L54); Go `client.go` (L386), `bargein.go` | Latest transcript cancels/merges in-flight turn | Natural conversation, latency |
| Dead-air watchdog | Go `watchdog.go` | Holding line on silence | Call quality |
| AMD gate | Go `amd_gate.go`, `call_control.go` | Voicemail detection defers/ends brain | Cost, correctness |
| Semantic turn detector | Go `semantic_turn` worker | EOU prediction | Turn boundary quality |
| Audit chain (v3) | `memory/audit.py`; `turn.py` (L754) | Per-turn reconstructable audit | Observability, compliance evidence |
| Decision log | `turn_decision_log.py` (L86) | Structured routing decision log incl. guards | Debugging routing |
| OTEL telemetry | `telemetry.py` | Spans + latency class | Performance observability |
| Brand override packs | `nlg.py` + `flows/reply_manifest.json` | Tenant/brand reply text overrides | Multi-brand deployment |
| Startup live-config validation | `startup_validation.py` | Fails fast on bad live creds | Deployment safety |
| Retrieval cache | `retrieval.py` (L25) | TTL/LRU KB cache | Latency/cost |

---

## 14. Gaps, Unknowns, and Risks

| Gap / Unknown | Evidence | Risk | Suggested follow-up question |
|---------------|----------|------|------------------------------|
| Refusal/dispute→link switch fragile when digression OFF | Pins only merged if `sot_digression` on (`turn.py` L1228); default off (`config.py` L125) | Missed intent switch (the original prod bug) | Should digression (or at least pinned merge) be enabled by default for SOT? |
| No persistent "unresolved high-risk label" | No such field (§6.6); dispute routes straight to transfer/close | If dispute missed, link can be sent over an unresolved dispute (Trace B) | Do we need an unresolved-dispute state that blocks payment prompts until resolved? |
| Dispute matcher coverage for paraphrases | `_sot_dispute_flow` L319 uses cue lists | Soft denials may fall through | Need transcript corpus review of denial phrasings |
| `_dispute_evidence` / `_frustration_turns` have no decay | §6.5–6.6 | Stale evidence across long calls | Should accumulators decay over turns/time? |
| No unified intent/label taxonomy or transition matrix | §7 | Hard to reason about/observe transitions | Do we want an explicit label layer (as in `MODEL_TRAINING_AND_ARCHITECTURE.md`)? |
| `enforce_safety_gate` defined but not wired | `config.py` L46; safety always runs anyway | Config confusion | Remove flag or wire it? |
| `SOT_DEFLECTION_OBJECTIONS` only used in digression-off path | `turn.py` L120–128, L1212 | Dead-ish config under digression | Confirm intended behavior |
| `WS_TURN_DEADLINE_MS` / `force_flow` absent from Go | Go contract review (§Go) | Docs reference features not in Go | Is turn-deadline enforced brain-side only? |
| Local VAD `ObserveAudio` not in production chain | `turn_manager.go` L172 only in tests | `LOCAL_VAD_ENABLED` may be a no-op | Should local VAD be wired for faster barge-in? |
| SOT disables compliance gate | `config.py` tenant (L315) `enforce_compliance_gate=false` | Outbound compliance checks skipped for SOT | Verify intended; ensure safety pre-empt covers gaps |
| Multi-turn e2e transition tests thin | §10 | Regressions in cross-intent routing may go uncaught | Add e2e golden tests for the transition matrix |
| Audit list TTL in Upstash | `store.py` — no expiry on audit/borrower | Unbounded growth | Define retention policy |
| `transfer_target` slot never set in inspected code | Read at `turn.py` L1360; falls back to default | Transfers always go to default target | Confirm target routing is intentional |

---

## 15. Files Inspected and Searches Performed

**Brain (`Collection/app`):** `main.py`, `config.py`, `startup_validation.py`, `telemetry.py`; `ws/{handler,session,borrower_resolve,borrower_context}.py`; `engine/{turn,command_gen,retrieval,executor,nlg,gate,safety,tracker,priority,robustness,slot_validation,conditions,actions,identity_gate,pipeline,turn_decision_log}.py`; `engines_p2/{emotion,risk,trust,persona,decision_overlay}.py`; `schemas/{state,flow,ws_contract,api}.py`; `memory/{store,composite,postgres_borrowers,upstash,audit}.py`; `clients/{llm_vertex,llm_groq,kb,kb_headers,whatsapp,transfer,tools,tools_sim,tools_stub,tools_live,tools_fixtures,sot_tools_sim,protocols}.py`; `sim/scripted_clients.py`; `flows/loader.py`, `flows/*.yml`, `flows/salary_on_time/*.yml`, `flows/reply_manifest.json`, `flows/flow_doc_map.json`.

**Go (`Websocket`):** `cmd/server/main.go`; `internal/media/{server,server_asterisk,session,asr_sink,sarvam_asr,sarvam_language,turn_manager,turn,semantic_turn,bargein,buffer,denoise_sink,amd_gate,tts,tts_reply_consumer,tts_session,elevenlabs_tts,sarvam_tts,egress_carrier,voicemail,watchdog}.go`; `internal/brain/{client,contract,bootstrap_sink,call_control}.go`; `workers/{denoise,amd,semantic_turn}/server.py`; `deploy/docker-compose.yml`.

**Tests:** `tests/golden/*` (sot_pre_closure, repair_layer, whatsapp, transfer, on_post_due, slot_validation, identity/payment/followup/refusal/dispute/adversarial/robustness/hardship/compliance FS suites, executor, handle_turn_e2e, bp14_gate); `tests/unit/*` (kb_client, command_gen, memory_tracker, startup_validation, persona_engine, borrower_resolve, flow_sim, sprint4_pipeline, latency, api, …); `tests/compliance/test_release_gate.py`; `tests/sim/*.json`; `tests/conftest.py`; `tests/fixtures/*`, `tests/helpers/*`.

**Search terms used:** turn, handle_turn, transcript, flow_stack, Frame, start_flow, cancel_flow, set_slot, clarify, human_handoff, cannot_handle, transfer, disposition, coerce, pinned, confidence_floor, dispute, accumulator, frustration, digression, priority, reorder, suppress, retrieval, KB, command_gen, executor, nlg, gate, safety, emotion, risk, trust, persona, whatsapp, hangup, end_call, sot_call_closed, session_start, chunk, done, barge, semantic_turn, flow_class, salary_on_time.

**Method:** Five parallel read-only subagents traced (1) the brain turn pipeline, (2) state & memory, (3) flows & routing guards, (4) the Go media server & workers, (5) external clients & tests. Findings cross-referenced.

**Not exhaustively inspected / requires runtime verification:**
- Full body of `engines_p2/decision_overlay.py`, `emotion.py` internals, and every FS golden assertion (sampled, not line-by-line).
- Live behavior of KB/LLM/WhatsApp/transfer endpoints (all stubbed in tests).
- Whether `WS_TURN_DEADLINE_MS` is enforced brain-side (deadline exists in `ws/handler.py` `asyncio.wait_for`, source of the value unclear).
- Exact matcher coverage of `_sot_dispute_flow` against real paraphrased denials.
- Line numbers are approximate for large files (`turn.py` ~1560 lines) and should be re-confirmed before editing.

---

*End of report. No application code was modified during this investigation.*









