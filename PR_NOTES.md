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
