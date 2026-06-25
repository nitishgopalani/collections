# Phase 1 — Collections Dialogue Engine (Complete)

**Status:** Phase 1 complete (Sprints 0–7)  
**Version:** `0.1.0`  
**Last updated:** June 2026

This document describes everything delivered in Phase 1: a working, RBI-oriented, auditable **text-in → text-out** collections dialogue engine. Phase 2 (trust, risk, persona, emotion intelligence engines) is additive on this spine and does not replace it.

---

## Table of contents

1. [Executive summary](#executive-summary)
2. [Core design principles](#core-design-principles)
3. [Architecture overview](#architecture-overview)
4. [Turn pipeline (`handle_turn`)](#turn-pipeline-handle_turn)
5. [Sprint delivery log](#sprint-delivery-log)
6. [Module reference](#module-reference)
7. [Flows and dialogue policy](#flows-and-dialogue-policy)
8. [Compliance and safety](#compliance-and-safety)
9. [Governed tools layer](#governed-tools-layer)
10. [Knowledge base and LLM](#knowledge-base-and-llm)
11. [Memory, state, and audit](#memory-state-and-audit)
12. [API surface](#api-surface)
13. [Configuration and tenancy](#configuration-and-tenancy)
14. [Observability and latency](#observability-and-latency)
15. [Testing and merge gate](#testing-and-merge-gate)
16. [Environment variables](#environment-variables)
17. [How to run](#how-to-run)
18. [Known limitations and Phase 2 scope](#known-limitations-and-phase-2-scope)
19. [Items pending compliance sign-off](#items-pending-compliance-sign-off)

---

## Executive summary

Phase 1 delivers a **FastAPI service** that accepts a borrower utterance per turn and returns a compliant reply, with full decision tracing. The engine follows a strict separation of concerns:

| Layer | Responsibility |
|-------|----------------|
| **LLM (Gemini)** | Understand utterance → structured **commands** only (no reply text, no policy) |
| **Deterministic code** | Priority, flow execution, tool calls, templated NLG, compliance gate |
| **Compliance gate** | Final authority on every outbound line — nothing bypasses it |
| **Safety pre-empt** | Runs first each turn; overrides all flows on crisis/vulnerability signals |

At the end of Phase 1:

- **`POST /turn`** runs the full pipeline via `handle_turn` in `app/engine/turn.py`.
- **`tests/compliance/`** is the **merge gate** — the compliance suite must stay green before any merge.
- **115+ automated tests** pass in CI mode (stub/simulate clients; live KB/Vertex/Upstash tests are env-gated).

---

## Core design principles

1. **LLM understands → code decides → gate has the final word.** The model never emits borrower-facing text on the live path.
2. **Templated NLG only** — replies come from flow YAML templates with slot interpolation and spoken-form helpers (Hindi amounts/dates for voice).
3. **≤ 1 LLM call per turn** on the live path (`command_gen.generate` only).
4. **High-recall safety** — false positives routing to a human are acceptable; false negatives on crisis/vulnerability are not.
5. **Append-only audit** — every turn produces a reconstructable chain (flows → commands → tools → gate → final reply).
6. **Fail-soft retrieval** — KB errors yield empty candidates → `clarify` path, not a hard failure.
7. **Per-tenant configuration** — call windows, attempt caps, prohibited phrases, vulnerability signals (v1: env-backed defaults).

---

## Architecture overview

```
                    ┌─────────────────────────────────────────────────────────┐
                    │                    POST /turn                          │
                    └─────────────────────────┬───────────────────────────────┘
                                              │
                    ┌─────────────────────────▼───────────────────────────────┐
                    │              handle_turn (app/engine/turn.py)            │
                    └─────────────────────────┬───────────────────────────────┘
                                              │
     ┌────────────────────────────────────────┼────────────────────────────────────────┐
     │ FIRST                                  │                                        │
     ▼                                        ▼                                        LAST │
 safety_preempt ──(early exit)──► gate ──► persist ──► audit                              │
     │                                                                                    │
     │  load_state / hydrate_from_borrower                                                │
     │  retrieval (KB) ──► command_gen (Gemini, 1×)                                     │
     │  tracker.apply ──► priority.reorder                                                │
     │  executor.run_async ──► actions/tools                                              │
     │  nlg.draft_reply (templates)                                                       │
     │  gate (compliance) ◄───────────────────────────────────────────────────────────────┘
     │
     └─► save_state / save_borrower / append_audit
```

**External clients** (swappable via env):

| Client | Modes | Purpose |
|--------|-------|---------|
| Memory | `stub` (in-memory) / `live` (Upstash Redis) | Call state, borrower record, audit log |
| KB | `stub` / `live` (Fonada FAISS API) | Flow candidate retrieval |
| LLM | `stub` / `live` (Vertex Gemini) | Command generation |
| Tools | `stub` / `simulate` / `live` | Governed READ/WRITE backend actions |

---

## Turn pipeline (`handle_turn`)

Implemented in `app/engine/turn.py`. Called from `app/main.py` for every `POST /turn`.

### Ordered stages

| Step | Module | Notes |
|------|--------|-------|
| 1 | `memory.load_state` | Optimistic concurrency via `state.version` |
| 2 | `memory.load_borrower` | Durable borrower profile |
| 3 | `tracker.hydrate_from_borrower` | Loan fields + `compliance_flags` into slots |
| 4 | `turn_meta.call_date` | Optional; sets `slots.call_date` for gate replay/tests |
| 5 | **`safety.safety_preempt`** | If triggered: care-first reply, suspend recovery, **return early** (no KB/LLM) |
| 6 | `retrieval.retrieve_flow_candidates` | KB search + `[[flow:...]]` resolution; cached 5 min |
| 7 | `command_gen.generate` | Single Gemini call → validated `Command` list |
| 8 | `tracker.apply` | Turn event + commands applied; version bumped once |
| 9 | `priority.reorder` | Multi-signal stack: highest priority active, rest parked |
| 10 | `executor.run_async` | Walk flow stack; governed actions via `make_async_action_runner` |
| 11 | `nlg.draft_reply` | Template render / collect prompts / clarify fallback |
| 12 | **`gate.gate`** | 4-level compliance on draft reply; unconditional |
| 13 | `memory.save_state` + `save_borrower` | `sync_borrower_from_state` for loan + flags |
| 14 | `audit.build_turn_audit_record` | Full chain appended per borrower |

### Response mapping

`TurnResponse` fields are populated from the pipeline:

- `reply_text` — post-gate final text
- `transfer_to_human` — safety, gate CRITICAL, vulnerability flow, tool failure, etc.
- `actions_executed` — from executor (`ExecResult.actions_called`)
- `disposition` — from executor or `slots.disposition`
- `end_call` — from `ExecResult.end_call`
- `state_version` — optimistic concurrency token
- `audit_id` — UUID for the turn audit record

---

## Sprint delivery log

| Sprint | Theme | Delivered |
|--------|-------|-----------|
| **0** | Skeleton | FastAPI app, `/healthz`, `/turn` stub, schemas, in-memory store |
| **1** | Memory + tracker | `ConversationState`, `BorrowerRecord`, `apply`, hydrate, audit primitives |
| **2** | Flows + executor | YAML flow loader/validator, `executor.run`, conditions, collect/decide/utter/end steps |
| **3** | Governed tools | `ActionRegistry`, READ/WRITE tools, idempotency, `FakeToolClient` simulator, dispute flow |
| **4** | KB + command gen | Fonada KB client, `retrieve_flow_candidates`, Gemini command_gen, pipeline slice |
| **4a-fix** | KB hardening | Health path, headers, fail-soft, seed script, flow doc map |
| **5** | Priority + NLG | Priority ladder, park/resume, templated NLG, Hindi spoken-form, language ladder |
| **6** | Compliance gate | Safety pre-empt, 4-level gate, audit chain, `tests/compliance/` merge gate |
| **7** | Integration | Full `handle_turn`, OpenTelemetry, latency profiling, adversarial goldens, async executor |

---

## Module reference

### `app/engine/` — Core dialogue engine

| File | Role |
|------|------|
| `turn.py` | **`handle_turn`** orchestrator; safety early exit; persist + audit |
| `safety.py` | `safety_preempt`, `apply_safety_to_state` — runs **first** each turn |
| `gate.py` | `gate()` — runs **last** on every outbound line |
| `compliance_rules.py` | Shared phrase matching, call-window helper |
| `retrieval.py` | KB → `FlowCandidate` list; TTL cache |
| `command_gen.py` | Prompt build, parse/validate LLM JSON → `Command` |
| `pipeline.py` | `transcript_to_commands` (retrieval + command_gen) |
| `tracker.py` | `apply`, `hydrate_from_borrower`, `new_conversation_state` |
| `priority.py` | `reorder` — ladder: opt_out > vulnerable > dispute > hardship > ptp > refusal > reminder |
| `executor.py` | `run` (sync tests), `run_async` (live turn loop) |
| `actions.py` | `ActionRegistry`, `make_action_runner`, `make_async_action_runner` |
| `nlg.py` | `render`, `draft_reply`, `spoken_amount_hindi`, `spoken_date_hindi` |
| `conditions.py` | Boolean expression evaluation for flow branches |
| `latency.py` | `TurnLatencyProfile`, `StageTimer` — internal vs external ms |

### `app/clients/` — External integrations

| File | Role |
|------|------|
| `kb.py` | Fonada KB: `POST /search`, `GET /api/health` |
| `llm_vertex.py` | Vertex Gemini `complete()` with JSON mode |
| `tools.py` | Factory: stub / simulate / live |
| `tools_sim.py` | `FakeToolClient` — fixture borrowers `B_PAID`, `B_DUE`, `B_PARTIAL`, `B_VULNERABLE` |
| `tools_stub.py` | No-op tool client |
| `tools_live.py` | MPLS tools endpoint (contract `# DECISION NEEDED`) |
| `protocols.py` | `LLMClient`, `KBClient`, `ToolClient`, `MemoryStore` protocols |

### `app/memory/` — Persistence

| File | Role |
|------|------|
| `store.py` | `InMemoryMemoryStore`, `UpstashMemoryStore`, `StaleStateError` |
| `audit.py` | `TurnAuditChain`, `build_turn_audit_record`, `query_turn_audits_by_borrower` |
| `upstash.py` | Upstash Redis REST client |
| `pii.py` | PII masking for logs |

### `app/flows/` — Dialogue policy (YAML)

| File | Role |
|------|------|
| `loader.py` | `load_all_flows()` merges all `*.yml` |
| `validator.py` | Structural validation at load time |
| `promise_to_pay.yml` | PTP collect, validate, schedule follow-up |
| `dispute.yml` | Dispute collect, verify payment, park/resume parent, ticket |
| `pay_now.yml` | Payment link + prompt |
| `vulnerability.yml` | Route vulnerable → care-first utter |

### `app/` — Config, API, telemetry

| File | Role |
|------|------|
| `main.py` | FastAPI app, `/healthz`, `/turn` → `handle_turn` |
| `config.py` | `Settings`, `TenantConfig`, `tenant_config()` |
| `compliance_defaults.py` | RBI-sensible defaults (`# DECISION NEEDED` for legal sign-off) |
| `telemetry.py` | OpenTelemetry spans: `handle_turn`, retrieval, command_gen, executor, gate |
| `schemas/` | Pydantic models: API, state, command, flow, compliance |

### `app/engines_p2/` — Phase 2 placeholder

Empty package scaffold for future intelligence engines (trust, risk, persona, emotion).

---

## Flows and dialogue policy

Flows are **declarative YAML** — no business logic in Python except the generic executor.

### Active flows (v1)

| Flow | Priority key | Purpose |
|------|--------------|---------|
| `promise_to_pay` | `ptp` | Collect `ptp_date`, validate window (≤14 days), schedule follow-up |
| `dispute` | `dispute` | Collect `dispute_reason`, verify payment, park/resume parent, raise ticket |
| `pay_now` | `reminder` | Create payment link, prompt immediate payment |
| `vulnerability` | `vulnerable` | Local `route_vulnerable` → care utter + transfer |

### Executor step types

- **`collect`** — Pause until slot filled (next turn via `set_slot` command)
- **`action`** — Governed tool or local action (`validate_ptp`, `evaluate_resume`, etc.)
- **`decide`** — Branch on slot conditions
- **`utter`** — Set `reply_id` for NLG template
- **`end`** — Pop flow frame

### NLG

- Templates in flow YAML under `responses:` with optional `language: hi | en | hinglish`
- Round-robin variant selection via `state.attempts`
- Voice channel: spoken-form for amounts (`₹5000` → `paanch hazaar rupaye`) and ISO dates
- Collect prompts: `ask_ptp_date`, `ask_dispute_reason`; clarify: `clarify_general`

---

## Compliance and safety

### Safety pre-empt (`app/engine/safety.py`)

Runs **before** retrieval and LLM. On trigger:

- Sets `compliance_flags.vulnerable = true`
- Suspends recovery, suppresses dunning
- Returns care-first reply + `transfer_to_human = true`
- **No KB or LLM call** on this path

Signals: configurable per tenant; defaults include hospital, suicide ideation, job loss, distress phrases (see `compliance_defaults.py`).

### Compliance gate (`app/engine/gate.py`)

Runs on **every outbound line** after NLG. Levels: `LOW | MEDIUM | HIGH | CRITICAL`.

| Check | Typical outcome |
|-------|-----------------|
| Critical inbound (harassment, fraud, threat complaint) | CRITICAL → block + safe fallback + transfer |
| Vulnerable + collection pressure | CRITICAL → care-first |
| Outside call window | HIGH → silent (no contact) |
| Opt-out active | HIGH → silent |
| Daily attempt cap exceeded | HIGH → silent |
| Dispute hold + collection pressure | MEDIUM → block pressure, safe fallback |
| Prohibited outbound phrase | CRITICAL → modify/block + safe fallback + transfer |

Gate clock: uses wall clock by default; when `slots.call_date` is set (e.g. via `turn_meta.call_date`), uses that date at 10:00 local for window checks (replay/tests).

### Merge gate

```bash
pytest tests/compliance/ -m compliance
```

12 tests covering prohibited language, dispute-hold, out-of-window, opt-out, attempt cap, safety pre-empt, adversarial threat block, audit reconstructability.

---

## Governed tools layer

### Tool modes (`TOOLS_MODE`)

| Mode | Client | Use |
|------|--------|-----|
| `stub` | `tools_stub` | CI default for tools ping |
| `simulate` | `FakeToolClient` | Deterministic fixtures, WRITE idempotency, error injection |
| `live` | `tools_live` | MPLS HTTP endpoint |

### Action → tool mapping

| Flow action | Tool | Type |
|-------------|------|------|
| `verify_payment` | `check_last_payment` | READ (cached per turn) |
| `create_payment_link` | `create_payment_link` | WRITE (idempotent) |
| `raise_dispute_ticket` | `raise_dispute_ticket` | WRITE |
| `schedule_followup` | `schedule_followup` | WRITE |
| `log_disposition` | `log_disposition` | WRITE |

### Local actions (no external call)

`validate_ptp`, `route_vulnerable`, `evaluate_resume`, `drop_dispute_resume_parent`, `drop_for_payment_found`

### Simulator fixtures

| Borrower ID | Scenario |
|-------------|----------|
| `B_PAID` | Payment on record; dispute → verify finds payment → handoff |
| `B_DUE` | Outstanding EMI; standard collection path |
| `B_PARTIAL` | Partial payment history |
| `B_VULNERABLE` | Tool error injection for failure paths |

---

## Knowledge base and LLM

### KB (Fonada FAISS API)

- Search: `POST /search` with client API key (never admin key)
- Health: `GET /api/health`
- Chunks tagged `[[flow:name]]` or resolved via `flow_doc_map.json`
- Retrieval cache: 5-minute TTL, max 128 entries, keyed by tenant + normalized transcript

### LLM (Vertex Gemini)

- Model: `GEMINI_MODEL_ID` (default `gemini-2.5-flash`)
- Output: JSON array of commands only
- Valid commands: `start_flow`, `set_slot`, `cancel_flow`, `clarify`, `human_handoff`, `cannot_handle`
- Malformed / unknown → `clarify`
- Relative dates resolved to ISO (`kal`, `parso`, etc.) using `call_date` / today

### Stub behavior (CI)

- `KB_STUB=true` → empty retrieval → clarify path
- `LLM_STUB=true` → `[]` → clarify path

---

## Memory, state, and audit

### Conversation state (`ConversationState`)

- Keyed by `call_id` in memory store
- `flow_stack` — active/parked frames with `step_index`
- `slots` — extracted facts + hydrated loan fields + compliance flags
- `events` — append-only turn/command/action log
- `version` — incremented on each `tracker.apply`; `StaleStateError` on conflict (HTTP 409)

### Borrower record (`BorrowerRecord`)

- Keyed by `borrower_id`
- `loan` — `amount_due`, `dpd`, `bucket`
- `compliance_flags` — opt-out, dispute hold, vulnerable, attempt counters, etc.

### Audit (`TurnAuditChain`)

Every turn writes `kind=turn_audit` with:

- `candidate_flows`, `commands`, `actions_called`
- `safety_preempted`, `safety_reason`
- `gate_verdict`, `gate_level`, `gate_reason`
- `final_reply`, `transfer_to_human`
- `engine_internal_ms`, `external_ms`, `latency_ms`, `llm_calls`

Queryable via `query_turn_audits_by_borrower(memory, borrower_id)`.

---

## API surface

### `GET /healthz`

Returns service status and per-client connectivity:

```json
{
  "status": "ok",
  "stub_mode": true,
  "memory_stub_mode": true,
  "kb_stub_mode": true,
  "tools_mode": "simulate",
  "llm_stub_mode": true,
  "client_modes": {
    "kb": "stub",
    "tools": "simulate",
    "llm": "stub",
    "memory": "stub"
  },
  "clients": { "llm": true, "kb": true, "tools": true, "memory": true }
}
```

### `POST /turn`

**Request:**

```json
{
  "call_id": "call-uuid",
  "tenant_id": "default",
  "borrower_id": "borrower-uuid",
  "channel": "voice",
  "transcript": "kal payment kar dunga",
  "locale": "hi-IN",
  "turn_meta": { "call_date": "2026-06-25" }
}
```

**Response:**

```json
{
  "reply_text": "Theek hai sir, chhabbis June note kar liya...",
  "end_call": false,
  "transfer_to_human": false,
  "actions_executed": ["validate_ptp", "schedule_followup"],
  "disposition": null,
  "state_version": 1,
  "audit_id": "uuid"
}
```

**Errors:** `409 Conflict` on stale `state_version` (`StaleStateError`).

---

## Configuration and tenancy

### Global settings (`app/config.py` + `.env`)

See [Environment variables](#environment-variables).

### Per-tenant config (`TenantConfig`)

Resolved via `tenant_config(tenant_id)`. v1 uses env-backed defaults for a single `default` tenant.

Configurable per tenant:

- Call window hours and timezone
- Max attempts per day / week
- Prohibited outbound phrases
- Collection pressure phrases
- Vulnerability and distress signals
- Critical inbound phrases
- Fallback replies: safe, care-first, opt-out ack, clarify, silent
- Collect slot prompts (`ptp_date`, `dispute_reason`)
- Languages: `hi`, `en`, `hinglish`

Defaults live in `app/compliance_defaults.py` with `# DECISION NEEDED` markers for legal/compliance sign-off.

---

## Observability and latency

### OpenTelemetry (`app/telemetry.py`)

- Parent span: `handle_turn` with decision chain attributes
- Child spans: `retrieval`, `command_gen`, `executor`, `gate`
- Attributes: candidate flows, commands, actions, gate verdict/level, latency breakdown, `llm_calls`
- Graceful no-op if OTel SDK unavailable

### Latency profiling (`app/engine/latency.py`)

| Class | Counted toward |
|-------|----------------|
| **Engine-internal** | safety, tracker, priority, executor, nlg, gate, persist |
| **External** | KB retrieval, Gemini LLM, tool HTTP |

**Target:** engine-internal **p95 ≤ 700 ms** (stub/simulate path; verified in `tests/unit/test_latency.py`).

External latency is reported separately and not counted against the 700 ms budget.

---

## Testing and merge gate

### Test layout

| Directory | Count (approx.) | Purpose |
|-----------|-----------------|---------|
| `tests/compliance/` | 12 | **Merge gate** — gate, safety, audit |
| `tests/golden/` | 19 | Executor goldens, handle_turn e2e, adversarial e2e |
| `tests/unit/` | 80+ | Per-module unit tests, API, latency |
| `tests/fixtures/` | — | Borrower fixture re-exports |

### Pytest markers

| Marker | Requires |
|--------|----------|
| `compliance` | Nothing (always run in CI gate) |
| `live_kb` | `KB_STUB=false`, `KB_API_KEY` |
| `live_vertex` | `LLM_STUB=false`, Vertex credentials |
| `live_upstash` | `STUB_MODE=false`, Upstash creds |

### Recommended CI commands

```bash
# Merge gate (must be green)
pytest tests/compliance/ -m compliance

# Full CI suite (no live external deps)
pytest tests -m "not live_kb and not live_vertex and not live_upstash"

# Lint
ruff check app tests
```

### Current status (Phase 1 close)

- **115 passed**, 1 skipped, 9 deselected (live tests)
- Compliance: **12 passed**
- Ruff: clean

---

## Environment variables

Copy `.env.example` to `.env`. Key variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `STUB_MODE` | `true` | Forces in-memory memory when true |
| `TOOLS_MODE` | `simulate` | `stub` \| `simulate` \| `live` |
| `KB_STUB` | `true` | KB client stub |
| `LLM_STUB` | `true` | LLM client stub |
| `KB_BASE_URL` | Fonada API | KB host |
| `KB_API_KEY` | — | Client/agent key (not admin) |
| `GCP_PROJECT_ID` | — | Vertex project |
| `GCP_REGION` | `us-central1` | Vertex region |
| `GEMINI_MODEL_ID` | `gemini-2.5-flash` | Gemini model |
| `GOOGLE_APPLICATION_CREDENTIALS` | — | Path to Vertex SA JSON |
| `UPSTASH_REDIS_REST_URL` | — | Live memory store |
| `UPSTASH_REDIS_REST_TOKEN` | — | Live memory store |
| `CALL_WINDOW_START` / `END` | `08:00` / `19:00` | Tenant call window |
| `CALL_WINDOW_TIMEZONE` | `Asia/Kolkata` | Window timezone |
| `MAX_ATTEMPTS_PER_DAY` | `3` | Gate attempt cap |
| `STATE_TTL_SECONDS` | `14400` | Live state TTL (~4h) |

---

## How to run

### Windows

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env
uvicorn app.main:app --reload
```

### Unix

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
uvicorn app.main:app --reload
```

### Example turn

```bash
curl -X POST http://localhost:8000/turn \
  -H "Content-Type: application/json" \
  -d '{
    "call_id": "demo-call-1",
    "tenant_id": "default",
    "borrower_id": "B_DUE",
    "transcript": "kal payment kar dunga",
    "turn_meta": { "call_date": "2026-06-25" }
  }'
```

For deterministic PTP confirm with simulator tools, set `TOOLS_MODE=simulate` and use fixture borrower `B_DUE` with live KB/LLM or scripted clients in tests.

---

## Known limitations and Phase 2 scope

### Phase 1 limitations

1. **Single-tenant config in practice** — `tenant_config()` maps all tenants to env defaults; multi-tenant file/API config is future work.
2. **Tools live mode** — `tools_live.py` compiled but MPLS contract not fully exercised (`# DECISION NEEDED`).
3. **Weekly attempt cap** — defined in config; gate enforces daily cap in v1.
4. **NLG** — templates only; no generative reply text on live path (by design).
5. **Phase 2 engines** — `app/engines_p2/` is a placeholder; trust/risk/persona/emotion not implemented.
6. **BUILD_SPEC copy** — canonical spec remains `Collection_agent_spec`; `docs/BUILD_SPEC.md` may not be synced.

### Phase 2 (additive)

Per product roadmap, Phase 2 adds intelligence engines that **feed signals into** the existing spine (commands, priority, gate) rather than replacing it:

- Trust engine
- Risk engine
- Persona engine
- Emotion engine

Phase 1 compliance gate and safety pre-empt remain the final authority on outbound text.

---

## Items pending compliance sign-off

Marked `# DECISION NEEDED` in code — review before production:

| Item | Location |
|------|----------|
| Call-window hours (08:00–19:00 IST) | `compliance_defaults`, `TenantConfig` |
| Daily / weekly attempt caps | `config.py`, gate |
| Prohibited outbound phrase list | `compliance_defaults.py` |
| Vulnerability / distress signal lists | `compliance_defaults.py` |
| v1 languages (hi / en / hinglish) | `nlg.py`, `TenantConfig` |
| Safe fallback / care-first reply text | `compliance_defaults.py` |
| GCP region and Gemini model ID | `.env.example`, `config.py` |
| MPLS live tools API contract | `tools_live.py` |

---

## Document history

| Date | Change |
|------|--------|
| June 2026 | Phase 1 complete — initial documentation after Sprint 7 |

For the original build specification, see `Collection_agent_spec` at the repository root.
