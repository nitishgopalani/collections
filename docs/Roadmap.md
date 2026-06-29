# Collections Engine — Master Program Plan (4 lanes, scale-aware)

**Purpose:** Single source of truth for Cursor / agent planning. Read this before starting any sprint.  
**Repo:** `nitishgopalani/collections` · active branch: `Nitish`  
**Last updated:** June 2026

---

## Scale target

~**50 lakh calls/day** in a 6–7h window → ~214 calls/s avg (430–640 peak), ~1.7k `/turn` QPS avg (3.4–5k peak), ~40M turn-events/day, ~40–58k concurrent sessions.

**Infra assumptions:** Kafka ingest, internal Redis, in-house ASR/TTS. **LLM:** Gemini now → in-house cutover later (**SH-5**) after low-scale test.

---

## Invariants (all lanes)

| Rule | Detail |
|------|--------|
| Gate absolute | No override path can bypass `app/engine/gate.py` |
| Templated NLG only | Brands edit copy (`reply_id`); never flow logic or free-form LLM text to borrowers |
| Build practice | Goal → Scope → Tasks → Tests → DoD; compliance suite must stay green; commit on green |
| Architecture | Compliance brain = **Python** behind `/turn`; **Go** = telephony sessions + Kafka consumers; engine stateless/autoscaled |
| SQL ownership | All Supabase migrations handed over (Dev-first); agent proposes, human applies |

---

## Foundation already shipped (do not re-build)

| Phase | Scope | Status | Head commit area |
|-------|--------|--------|------------------|
| **Phase 1** | Sprints 0–7 — core `/turn` spine, flows, gate, tools, KB, memory | ✅ Complete | `PHASE_1_COMPLETE.md` |
| **Phase 2** | Sprints 8–13 — trust, risk, persona, emotion, overlay, recovery | ✅ Complete | `PHASE_2_COMPLETE.md` |
| **FS-1** | Identity verification gate | ✅ `identity_verification.yml` | |
| **FS-2** | Payment breadth (partial, initiated, dues, alt channel) | ✅ | |
| **FS-3** | Hardship + vague PTP | ✅ | |
| **FS-4** | Compliance & handoff (opt-out, third party, fraud, lawyer, …) | ✅ | |
| **FS-5** | Robustness + consolidated QA (91-scenario coverage report) | ✅ | `FS5_PILOT_COVERAGE_REPORT.md` |
| **FS-6** | Follow-up flows (PTP, link nudge, callback) | ✅ `ffd6f00` | |
| **FS-7** | Refusal & negotiation → human | ✅ `45f0c74` | |
| **FS-8** | Dispute breadth (amount, NACH, double charge, …) | ✅ `91b930f` | |
| **EB-1** | Turn audit instrumentation (reply_id/variant/lang/tone + agent/pack/manifest) | ✅ `ec57313` | |

**Test baseline (June 2026):** 302 passed, 7 skipped (`STUB_MODE=true`, Python 3.13). Compliance suite = merge gate.

**Design doc (not implemented):** [BRAND_CONTROLLED_DIALOGUE.md](./BRAND_CONTROLLED_DIALOGUE.md) — multi-tenant copy, Supabase packs, Fonada dashboard tab.

---

## Critical path

```
EB-1 ✅ → BP-1.2 → BP-1.3 → BP-1.4 → EB-4 → AN-1 → AN-2 → AN-3
                ↘ BP-2.x (Supabase) in parallel ↗
```

**Order-of-magnitude levers:** SH-1 (flow cache), SH-3 (LLM short-circuit).  
**Analytics UI gate:** AN-4 (DPDP retention + org-scoped access).  
**Parallel-safe:** Backend BP-1.x ∥ DB BP-2.x ∥ scale-hardening SH-x ∥ Telephony prep BP-6.x.

---

## Lane 1 — Backend · Engine (Python)

| Sprint | Title | Depends | Status |
|--------|-------|---------|--------|
| BP-1.1 | `reply_id` catalog / manifest in CI (~90 response ids, lock flags) | — | 🔶 Partial — `MANIFEST_VERSION` in `app/flows/manifest.py`; CI artifact + lock list **TODO** |
| **EB-1** | Instrument audit chain (`reply_id`, `variant_index`, `language`, `tone_register`, `agent_id`, `pack_id`, `manifest_version`) | — | ✅ Done `ec57313` |
| **BP-1.2** | `merge_response_overrides()` + multi-variant merge rules | BP-1.1 | ▶ **NOW** |
| BP-1.3 | `agent_id` loader interface + Supabase fixture (consumes EB-1 fields) | EB-1, BP-1.2 | |
| BP-1.4 | Merge into live render path + gate-invariant test | BP-1.3 | |
| EB-4 | Audit retention + `AuditSink` egress seam (fire-and-forget → Kafka) | EB-1 | |
| EB-2 | Live integrations: KB key, tools/MPLS, Gemini LLM | — | |
| EB-3 | Compliance sign-off on 8 `DECISION NEEDED` values | — | |
| EB-5 | Secret rotation + canonical-spec verify + go-live closeout | EB-2..4 | |
| SH-1 | **Flow cache** — load/validate once at startup; hot-reload on publish (kill per-turn `load_all_flows()`) | — | Scale |
| SH-2 | **Native Redis** + pipelined writes (replace Upstash REST), Cluster-ready | — | Scale |
| SH-3 | **LLM short-circuit + command cache** — rules-first; skip model on deterministic turns | — | Scale |
| SH-4 | Stateless autoscale + **5k turns/s load test** (sustained + burst, p99 budget) | SH-1..3 | Scale |
| SH-5 | **In-house LLM cutover** (replace Gemini) — post low-scale test | SH-4 | Scale |

**Hot-path fixes (verified in repo today):**

- SH-1 removes per-turn YAML reparse in `handle_turn` (`load_all_flows()` every turn).
- SH-2 collapses ~6 Redis REST round-trips/turn into pooled + pipelined RESP.
- SH-3 cuts the unconditional per-turn `command_gen` LLM call.
- EB-4 makes audit a Kafka produce, not an unbounded `RPUSH`.

---

## Lane 2 — DB · Supabase + Warehouse

| Sprint | Title | Depends | Status |
|--------|-------|---------|--------|
| BP-2.1 | `collections_dialogue_*` schema (packs + overrides, multi-variant jsonb) | BP-1.1 | |
| BP-2.2 | Agent binding (`agent_mode='collections_engine'` + `collections_pack_id`) | BP-2.1 | |
| BP-2.3 | RLS — per-org isolation (`organizations.id`) | BP-2.1 | |
| BP-2.4 | Mandatory-lock + slot integrity as DB constraints | BP-2.1, BP-1.1 | |
| BP-2.5 | Version-pinning + auto-inherit defaults (notify, never block) | BP-2.1 | |
| BP-3.1 | `validate` edge fn (schema / allowlist / slot / phrase) | BP-2.x | |
| BP-3.2 | Gate dry-run (server-side invariant) | BP-3.1, BP-1.4 | |
| BP-3.3 | Publish + archive + cache-invalidate webhook | BP-3.2 | |
| BP-3.4 | Tiered compliance-review queue | BP-3.1 | |
| AN-1 | **Kafka `collections.turn_events` consumer → columnar warehouse** (NOT per-turn Postgres insert) | EB-1, EB-4 | Scale |
| AN-2 | Call-grain `collections_call_outcomes` fact (partitioned, ~5M rows/day) | AN-1 | |
| AN-3 | Aggregation / rollup / query layer (funnel, variant A/B, lang-tone, compliance, signals, ops) | AN-2 | |
| AN-4 | **DPDP** retention, minimization & org-scoped access — gates the UI | AN-1..3 | |

**Store split at 5M/day:** Supabase Postgres = config/governance/publish + `call_outcomes`; warehouse = high-volume `turn_events`; Kafka = seam between engine and both.

---

## Lane 3 — Frontend · React (Fonada AI-platform)

Repo path: `C:\Users\nitis\source\repos\AI_Agents\AI-platform`

| Sprint | Title | Depends | Status |
|--------|-------|---------|--------|
| BP-4.1 | Feature scaffold + Sidebar route + agent/pack picker | BP-2.x | |
| BP-4.2 | Grouped `reply_id` editor (categories, lang/tone tabs, add-variant, lock UI) | BP-4.1, BP-3.1 | |
| BP-4.3 | Tone/strategy presets | BP-4.2 | |
| BP-4.4 | Live gate preview + publish UX + review-queue surface | BP-4.2, BP-3.x | |
| BP-5.1 | Compliance-officer governance view | BP-3.x | |
| BP-5.2 | 91-scenario brand simulator | BP-4.x, BP-1.4 | |
| BP-5.3 | Bounded intelligence-engine knobs (gate-validated) | BP-5.2 | |
| BP-5.4 | Audit / assurance reporting | BP-5.1 | |
| AN-5 | Analytics scaffold + outcome funnel + operational | AN-3, AN-4 | |
| AN-6 | Variant A/B + language/tone effectiveness (deep-link from editor) | AN-5, BP-4.2 | |
| AN-7 | Engine-signal + compliance dashboards (feeds BP-5.4) | AN-5, BP-5.4 | |

---

## Lane 4 — Telephony · Go (separate repo)

| Sprint | Title | Depends | Status |
|--------|-------|---------|--------|
| BP-6.1 | `collections_engine` routing + tenant resolution | BP-1.4, BP-2.2 | |
| BP-6.2 | ASR → `/turn` → TTS wiring + live cache invalidation | BP-6.1, BP-3.3 | |
| GO-1 | **Session-manager scale** — 40–58k concurrent media sessions | BP-6.2 | Scale |
| GO-2 | **Kafka consumers** (turn-events ingest + cache-invalidate) | BP-6.2 | Scale |
| BP-6.3 | End-to-end QA (full call, pilot pack) | BP-6.2 | |
| GO-3 | Media load test at peak | GO-1 | Scale |
| BP-6.4 | Pilot with one anchor brand | BP-6.3, BP-5.4 | |

---

## Agent quick-reference (what to do next)

1. **Next sprint:** **BP-1.2** — implement `merge_response_overrides()` in Collection repo; finish **BP-1.1** CI catalog artifact if not done in same PR.
2. **Do not touch:** `gate.py`, flow YAML (unless explicitly scoped), Gemini/LLM wiring (unless EB-2/SH-5).
3. **After BP-1.4:** EB-4 audit egress unblocks AN-1 warehouse ingest.
4. **Frontend work** waits on BP-2.1 schema; scaffold BP-4.1 only after BP-2.2 agent binding is agreed.
5. **Commit message pattern:** `EB-1: …`, `BP-1.2: …`, `feat: FS-x …` (match existing history).

---

## Companion docs

| Doc | Use |
|-----|-----|
| [PHASE_1_COMPLETE.md](./PHASE_1_COMPLETE.md) | Core engine architecture |
| [PHASE_2_COMPLETE.md](./PHASE_2_COMPLETE.md) | Intelligence engines |
| [GO_LIVE_CHECKLIST.md](./GO_LIVE_CHECKLIST.md) | Pre-prod verification |
| [BRAND_CONTROLLED_DIALOGUE.md](./BRAND_CONTROLLED_DIALOGUE.md) | Brand copy + multi-tenant design |
| [FS5_PILOT_COVERAGE_REPORT.md](./FS5_PILOT_COVERAGE_REPORT.md) | 91-scenario matrix |

---

## Open decisions

| # | Question | Default (until product says otherwise) |
|---|----------|----------------------------------------|
| 1 | Warehouse engine for `turn_events` | TBD (ClickHouse / BigQuery / …) |
| 2 | BP-1.1 lock list — which `reply_id`s are brand-uneditable? | Identity + compliance handoff lines |
| 3 | Publish self-service vs compliance queue | Tiered (BP-3.4) |
| 4 | `tenant_id` migration from `"DefaultTenant"` | Org slug / explicit column |
| 5 | ConsentChain mechanism | AN-4 scope |

---

**~45 sprints total** across 4 lanes. EB-1 complete; critical path advances on **BP-1.2 → BP-1.3 → BP-1.4**.
