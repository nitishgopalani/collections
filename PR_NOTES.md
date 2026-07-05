# Consult-result push — deliver the outcome to a silent caller

Branch: `feature/consult-result-push` (from `feature/booking-confirm-bot`)

## Why (reviewer finding)

During hold the customer is silent (they hear MOH), so no `turn` messages
arrive from the go-server — and the consult result was only checked at the
START of a turn. A confirmed booking could sit in `CONSULT_RESULTS`
undelivered until the caller happened to speak. Fixed WITHOUT changing the
go-server turn model.

## What changed

- **`app/ws/session.py`** — `BrainWSSession` gained `send_lock`
  (`asyncio.Lock`) and `consult_watch_task`. The lock serializes the outbound
  chunk/flow_class/done sequence; both the prompt-turn emission and the push
  emission take it, so frames from the two paths can never interleave.
- **`app/ws/handler.py`** — after a prompt turn that leaves a consult
  pending, `_ensure_consult_watcher` spawns ONE `_consult_result_watcher`
  task per session. Every `CONSULT_PUSH_POLL_S` (2s) it checks the pending
  consult; on a decided outcome (property leg posted a result, orchestrator
  says the leg failed, or the `CONSULT_PUSH_BUDGET_S` (60s) budget ran out →
  forced failure) it emits the relay as an **unsolicited turn**
  (`turn_id=consult-push-<hex>`, `disposition=CONSULT_RELAYED`) through the
  existing chunk/flow_class/done path under `send_lock`. Interleaving rule:
  while `session.inflight_turn_id` is set the watcher NEVER consumes the
  result — it stays available for that turn's own pending-consult check, and
  the watcher picks it up on a later tick only if the turn didn't. The
  watcher exits when a caller turn consumed the result, and is cancelled on
  session_end/disconnect (same finally block that drops prompt history).
- **`app/engine/prompt_agent.py`** — the turn-path check was refactored into
  a shared `_take_result` core; new watcher-facing helpers:
  `has_pending_consult`, `take_consult_result(force_fail=)` (no poll counting
  — the watcher owns its own budget) and `build_consult_relay` (injects
  `[CONSULT RESULT: ...]` into history and asks the persona LLM for the
  natural relay; falls back to canned lines when the LLM fails). Turn-path
  behaviour is unchanged.

## Test results (real output, 2026-07-05)

`pytest tests/unit/test_prompt_ws_integration.py tests/unit/test_prompt_agent.py -q`

```
19 passed, 1 warning in 1.41s
```

New tests (`tests/unit/test_prompt_ws_integration.py`):

- `test_silent_customer_still_hears_consult_result` — consult started, the
  customer sends NO more turns; the property outcome lands in
  `CONSULT_RESULTS`; the confirmed relay arrives as an unsolicited
  `consult-push-*` turn within the (shrunk) poll budget, `consult_finish`
  fires with `confirmed=yes`, and the relay LLM call saw the injected
  `[CONSULT RESULT: ...]` line.
- `test_silent_customer_gets_failure_push_when_budget_expires` — no result
  ever arrives; after the budget the caller hears the could-not-reach
  fallback and `consult_finish(outcome=failed)` fires.
- `test_push_never_interleaves_with_inflight_turn` — the result lands while a
  turn is mid-flight (held open deterministically by blocking the
  `consult_status` poll on a threading.Event). Asserts strict frame order:
  every frame up to the turn's `done` belongs to the turn (hold reply), every
  frame after belongs to the push turn — no interleaving — and the push still
  carries the confirmed outcome.

## Assumed, not verified

- Go-server handling of an unsolicited turn_id: `dispatchInbound` plays
  chunks for any non-superseded turn_id and `done` for a turn it never sent
  is a no-op on its inflight bookkeeping (verified by reading
  `Websocket/internal/brain/client.go`, not by a live call).
- Real MOH hold timing (poll 2s / budget 60s) — only exercised with shrunk
  test values.

---

# Booking-confirm bot — prompt-mode agent + consult hand-off

Branch: `feature/booking-confirm-bot` (base: `main` @ c1aca70)

Adds a second agent mode next to the flow engine: **PROMPT MODE** — ASR text
goes straight to the LLM (existing `llm_vertex` client, no new dependency)
with a per-tenant system prompt; the reply goes straight to TTS through the
SAME EB-6 chunk/flow_class/done contract. Built for the OYO booking-confirm
bot with a hold-and-consult hand-off to the ari-orchestrator; fully testable
with ZERO telephony (mocked LLM + mocked orchestrator client).

## What changed

- **Config** (`app/config.py`): `TenantConfig.agent_mode` (`flow` default |
  `prompt`), `prompt_personas` (name → system prompt), `default_persona`.
  New tenant **`booking-confirm`** in `_TENANT_ROUTING_DEFAULTS`
  (extends Phase C's registry — not duplicated): `mode=prompt`, locale
  `hi-IN`, personas `persona_customer` (OYO support, Hinglish, collects
  booking ID/hotel/guest, no tools) and `persona_property` ("Amit from OYO"
  verifying a booking with the owner). Prompts live in config, not code.
- **`app/engine/prompt_agent.py`** (new): in-memory per-session history
  (keyed by session_id, capped, dropped on session end/disconnect), calls
  `llm.complete(system_prompt, history + user turn, json_only=False)`.
  Persona = session `agent_id` when it names one, else `default_persona`.
- **Branch point** (`app/ws/handler.py::_run_turn`): tenants with
  `agent_mode == "prompt"` route to `_run_prompt_turn` instead of
  `handle_turn` — same register/cancel/deadline handling, same outbound
  chunking (`chunk_reply_for_tts`), `flow_class=Default`, then `done`.
  The connector/go-server sees no difference.
- **Routing**: `client_id="booking-confirm"` already resolves via Phase C's
  `resolve_session_tenant`; tenant defaults supply mode + `hi-IN`. Added a
  TEST_MODE exception: an explicit `client_id`/`tenant_id` naming a
  prompt-mode tenant wins even on the TEST_MODE server (flow-engine test
  routing unaffected — those sessions carry neither field).
- **Cross-leg consult state (Part 3)**: customer-persona LLM signals a
  consult with `<consult booking_id=... hotel=... guest=... phone=...>` at
  the end of its reply. `prompt_agent` strips it, calls the orchestrator
  client (`consult_start`) and holds the caller ("line par bane rahiye").
  The property leg's LLM reports `<consult_result booking_id=...
  confirmed=yes|no note=...>`; recorded in `prompt_agent.CONSULT_RESULTS`
  keyed by booking_id (the shared correlation id), spoken reply stripped,
  `end_call=true`. On the customer's next turn the result is injected as a
  `[CONSULT RESULT: confirmed=..., note=...]` system message (LLM relays it
  naturally) and `consult_finish` fires. While waiting: canned hold reply
  (no LLM round-trip); `consult_status` is polled and a `failed` leg (telco
  480) or poll-budget exhaustion injects `confirmed=unknown` → the LLM
  speaks the "couldn't reach the property" fallback.
- **Orchestrator client** (`app/clients/orchestrator.py`): added
  `consult_start`, `consult_finish`, `consult_status` (+ `_get` helper) for
  the new `/v1/consult/*` API (see ari-orchestrator `feature/consult-flow`).

## Test results (real output, 2026-07-05)

`pytest tests/unit/test_prompt_agent.py tests/unit/test_prompt_ws_integration.py -q`

```
16 passed, 1 warning in 0.80s
```

Targeted ws/tenant/actions modules (`test_prompt_agent`,
`test_prompt_ws_integration`, `test_eb6_ws_contract`, `test_ws_streaming`,
`test_phase_c_multitenancy`, `test_test_mode_ws`,
`golden/test_orchestrator_actions`):

```
1 failed, 47 passed, 1 warning in 2.09s
FAILED tests/unit/test_eb6_ws_contract.py::test_brain_ws_turn_emits_chunk_flow_class_done
```

The one failure is the **pre-existing time-of-day dependent** eb6 test
(`gate: silent:outside_call_window` — run at ~00:40 IST, outside the default
08:00–19:00 window). It fails identically on `main` @ c1aca70 and is not
touched by this branch.

Full collect: `pytest --collect-only -q` → **591 tests collected** (no
collection errors). `ruff check` on all files touched by this branch: clean
(3 pre-existing errors elsewhere in `handler.py` etc. unchanged).

Key scripted QA (Part 4): `test_two_session_consult_round_trip` runs two real
`/ws/brain` sessions (customer + property personas) with a scripted LLM and a
mocked orchestrator client: customer triggers the consult marker → property
session records `consult_result` → customer session injects and relays it —
and asserts the two sessions used **different** persona system prompts.

## Assumed, not verified (for the live smoke test)

- **`client_id` plumbing go-server → brain**: the connector already sends
  `client_id` to the go-server, but the go-server's brain client
  (`Websocket/internal/brain`) does NOT forward it (no field on its
  `SessionStartPayload`); it forwards `tenant_id` (from session params /
  `BRAIN_TENANT_ID`). The inbound smoke on 1725617002 therefore needs either
  a one-line go-server change (forward `client_id`) or per-DID metadata
  `tenant_id=booking-confirm` from the connector — plus the dialplan entry.
  Both handler paths (client_id and tenant_id) are already accepted brain-side
  (incl. TEST_MODE). Not run tonight; no server deploy of this branch yet.
- **Consult destination/channel**: the property phone comes from the consult
  marker's `phone=` attr or `CONSULT_PROPERTY_NUMBER` env;
  `customer_channel_id` from `borrower_context.channel_id` (falls back to
  session_id). Real wiring of the Asterisk channel id into borrower_context
  is connector-side work, mocked in tests.
- Outbound origination is still blocked at the telco (480) — by design the
  consult failure path covers exactly this and speaks the fallback.


# Phase C — Multi-tenancy (brain-side, collections)

Branch: `feature/phase-c-multitenancy`

Phase C makes the brain WebSocket path tenant-aware so the `asterisk-connector`
can send `client_id` on `session_start` (it already does on the wire) and the
brain threads that identity through turns, routing defaults, and isolation
guards — without changing the outbound EB-6 turn protocol.

## What changed

### C1 — Thread real tenant/client id through the brain WS session

- `SessionStartMessage` gains optional `client_id: str = ""` (backward compatible;
  callers that omit it still validate).
- `handle_brain_websocket` resolves tenant via `resolve_session_tenant()`:
  1. non-empty `client_id` (connector tenant id)
  2. agent-id routing (`resolve_agent_routing`) — pre-Phase-C behaviour
  3. explicit `tenant_id` on session_start
  4. `settings.default_tenant_id`
- Logs which source was used at INFO. `BrainWSSession.tenant_id` receives the
  resolved value (no longer hardcoded at accept time).
- `tenant_config(...)` / `safe_fallback_reply` lookup moved to **after**
  `session_start` is parsed; pre-session window uses `default_tenant_id` only.

### C2 — Tenant → flow-pack / agent / locale routing

- Extended `TenantConfig` + config-backed `_TENANT_ROUTING_DEFAULTS` registry
  (no DB). Per-tenant `default_pack_id`, `default_agent_id`, `default_locale`.
- `resolve_session_defaults()` fills gaps when session_start omits pack/agent/
  locale; explicit session_start values always win.
- Example tenants in config: `acme_collections` (en-IN), `globex_recoveries`
  (ta-IN), `salary_on_time` (hi-IN).

### C3 — Isolation guards (brain-side only)

- `tenant_id` already flows into `TurnRequest` → `handle_turn` → audit/CDR
  (`turn_trace`, `_persist_turn`, memory load/save). WS handler logs now include
  `tenant_id` on session_start, session_end, disconnect, turn deadline/cancel,
  and concurrency rejection.
- `TenantSessionRegistry` (`app/ws/tenant_limits.py`): in-process per-tenant
  active-session counter. At `session_start`, if `max_concurrent_sessions > 0`
  and the tenant is at cap, the socket is closed (1013) with a logged reason.
  Slot released in the handler `finally` block on `session_end` / disconnect.
  `max_concurrent_sessions = 0` means unlimited (default — single-tenant unchanged).

## Backward-compat guarantee

- **No `client_id` on session_start:** resolution falls through to the exact
  pre-Phase-C chain (agent routing → `tenant_id` field → `default_tenant_id`).
  Existing Go media-server / test callers that never sent `client_id` behave as
  before.
- **Outbound EB-6 contract unchanged:** no new brain→Go message types; turn
  protocol untouched.
- **TEST_MODE:** still pins to salary-on-time test routing; `client_id` is not
  honoured in TEST_MODE so deterministic test runs are unchanged.

## Test results

```
pytest tests/unit/test_phase_c_multitenancy.py -v  → 13 passed
pytest tests/unit/test_eb6_ws_contract.py tests/unit/test_ws_supersede.py \
       tests/unit/test_ws_streaming.py tests/unit/test_test_mode_ws.py \
       tests/unit/test_phase_c_multitenancy.py -v  → 20 passed, 2 failed*
pytest --collect-only -q  → 564 tests collected, 0 import errors
```

\* The two failures (`test_brain_ws_turn_emits_chunk_flow_class_done`,
`test_brain_ws_streams_gated_chunks_before_done`) reproduce on `main` without
Phase C changes (chunk `seq` assertion); not introduced by this branch.

## Deferred (not in this branch)

- **Connector/orchestrator-side isolation:** per-tenant API keys, isolated CDR
  storage, connector `metadata.agent_id` / `client_id` population beyond what
  the brain now accepts.
- **DB-backed tenant store:** tenant registry is config-map only; no admin API
  or runtime tenant CRUD.
- **Cluster-wide concurrency caps:** registry is in-process; multi-node brain
  deployments need a shared counter (Redis) for accurate caps.
- **Real cross-tenant leakage testing:** no infra to prove Redis/memory/CDR
  isolation across tenants at scale; brain-side attribution + cap guard only.
- **Orchestrator multi-tenancy:** `ari-orchestrator` remains tenant-agnostic
  (Phase C scope was collections/brain only).
