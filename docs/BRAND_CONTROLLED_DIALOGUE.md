# Brand-Controlled Dialogue — Architecture & Integration Guide

**Status:** Design doc (not yet implemented)  
**Last updated:** June 2026  
**Related:** [PHASE_1_COMPLETE.md](./PHASE_1_COMPLETE.md) · Fonada AI-platform (`C:\Users\nitis\source\repos\AI_Agents\AI-platform`)

This document describes how **brands** (Fonada tenants) control **what the collections agent says** — opening copy, identity verification scripts, flow responses, tone, and language — without editing the deterministic engine, compliance gate, or action registry.

---

## Table of contents

1. [Problem statement](#problem-statement)
2. [Layer model — what brands may vs may not control](#layer-model--what-brands-may-vs-may-not-control)
3. [Current state (Collection repo + Fonada platform)](#current-state-collection-repo--fonada-platform)
4. [Target architecture](#target-architecture)
5. [Multi-tenant data model (Supabase)](#multi-tenant-data-model-supabase)
6. [Brain runtime — loading and merging overrides](#brain-runtime--loading-and-merging-overrides)
7. [Publish pipeline (draft → validate → compliance → live)](#publish-pipeline-draft--validate--compliance--live)
8. [Frontend module (AI-platform integration)](#frontend-module-ai-platform-integration)
9. [Worked example — identity verification copy](#worked-example--identity-verification-copy)
10. [Telephony / edge wiring](#telephony--edge-wiring)
11. [Product decisions (recommended defaults)](#product-decisions-recommended-defaults)
12. [Implementation phases](#implementation-phases)
13. [Open questions](#open-questions)

---

## Problem statement

Fonada is multi-tenant: ~400+ organizations, each with their own agents, voice settings, and campaigns. The **Collections Dialogue Engine** (this repo) is the orchestration brain: ASR text in → gated templated reply out → TTS.

Brands need to:

- Customize **spoken copy** (Hindi / English / Hinglish) while staying RBI-compliant.
- Set **call opening** and **identity verification** wording that matches their brand voice.
- Tune **tone register** (standard, de-escalate, reassure) per response where the engine supports it.
- Do all of this from the **existing Fonada React dashboard** — a new sidebar tab, not a separate product.

They must **not** be able to:

- Bypass the compliance gate or safety pre-empt.
- Add new actions, change flow logic, or emit free-form LLM text to borrowers.
- Disable identity gating, dispute holds, vulnerability routing, or human handoff rules.

---

## Layer model — what brands may vs may not control

```
┌─────────────────────────────────────────────────────────────────────────┐
│  BRAND-CONTROLLED (upper layer)                                         │
│  • Response templates keyed by reply_id (per language / tone variant)   │
│  • collect_slot_prompts overrides (ask_ptp_date, ask_dispute_type, …)   │
│  • call_opening / first utterance mapping                               │
│  • Enabled flow palette (subset of platform flows)                        │
│  • Tenant policy knobs exposed in UI (call window, attempt caps*)         │
│  • Prohibited phrase *additions* (never removals of platform baselines) │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼ merged at runtime
┌─────────────────────────────────────────────────────────────────────────┐
│  PLATFORM-OWNED (lower layer — not brand-editable)                      │
│  • Flow YAML structure (steps, actions, decide branches, priority order)  │
│  • Action registry (verify_identity, apply_dispute_hold, route_to_human)│
│  • Compliance gate (app/engine/gate.py) — final veto on every line      │
│  • Safety pre-empt, identity gate, dunning suppression rules              │
│  • LLM command schema (understand only — never borrower-facing text)    │
│  • Tool contracts (payment verify, account lookup, etc.)                │
└─────────────────────────────────────────────────────────────────────────┘
```

\* Attempt caps and call windows may be **restricted** by platform maxima; brands configure within allowed bounds.

---

## Current state (Collection repo + Fonada platform)

### Collection brain (this repo)

| Component | Location | Today |
|-----------|----------|-------|
| Flow logic | `app/flows/*.yml` | Platform-shipped YAML; merged by `load_all_flows()` |
| Response copy | `responses:` blocks in same YAML files | ~80+ `reply_id` entries across flows |
| NLG | `app/engine/nlg.py` | `render(reply_id)` picks language/tone variant; slot interpolation only |
| Collect prompts | `TenantConfig.collect_slot_prompts` | Env-backed defaults via `tenant_config()` |
| Tenancy | `TurnRequest.tenant_id` | Accepted on `/turn` but **not** used to load per-tenant copy yet |
| Gate | `app/engine/gate.py` | Runs on final interpolated text; uses `TenantConfig` phrase lists |

Key NLG rule (unchanged in target design):

```python
# app/engine/nlg.py — render() never free-generates text
variants = flows.responses.get(reply_id)
variant = pick_variant(variants, preferred_language, tone_register)
return interpolate_template(variant.text, safe_slots, channel=channel)
```

### Fonada AI-platform (local repo: `AI_Agents/AI-platform`)

| Component | Location | Today |
|-----------|----------|-------|
| Sidebar | `src/components/Dashboard/Sidebar.tsx` | `menuItems` array — Dashboard, AI Agents, Agent Builder, Telephony, etc. |
| Routes | `src/App.tsx` | `/dashboard/agent-builder`, `/dashboard/agents`, … |
| Agent copy UI | `src/components/AgentBuilder/BasicInfo.tsx` | `first_message`, `system_prompt` (LLM agents) |
| Auth | `src/hooks/useAuth.ts` | Supabase session; org context via `user_profiles` elsewhere |
| Edge functions | `supabase/functions/*` | LLM / voice paths (`agent_brain`, `voice_ai`) — **not** wired to Collection `/turn` |
| Flow Builder | — | Present in prod DB (`conversation_flows`, `linked_flow_id`) but **not** in current local Sidebar |

### Fonada Supabase (prod DB via MCP)

| Entity | Role |
|--------|------|
| `organizations` | Brand/account (~421 active); billing, features |
| `user_profiles` | `organization_id`, `tenant_id` (mostly `"DefaultTenant"`) |
| `agent_configs` | Per-user agents: `first_message`, `voice_first_message`, `system_prompt`, `agent_mode` (`llm` \| `flow`) |
| `white_label_configs` | UI branding only (logo, colors) — **not** dialogue |
| Collection tables | **None yet** — no `collections_dialogue_packs`, turn audit, or borrower state in Supabase |

---

## Target architecture

```
  Fonada React Dashboard                    Supabase                         Collection Brain
 ┌──────────────────────┐              ┌─────────────────┐              ┌──────────────────────┐
 │ Collections Dialogue │  CRUD draft  │ dialogue_packs  │   publish    │ load_all_flows()     │
 │ (new sidebar tab)    │─────────────►│ (draft/live)    │─────────────►│ + merge_overrides()  │
 │                      │  validate    │ response_ovr    │   webhook/   │ handle_turn()        │
 │ Agent picker         │─────────────►│ agent_configs   │   poll cache │ gate(final text)     │
 └──────────────────────┘              └────────┬────────┘              └──────────▲───────────┘
                                                  │                                    │
  Go Telephony / Edge middleware ─────────────────┴── tenant_id + agent_id + /turn ────┘
```

**Principles:**

1. **Platform YAML remains source of truth for flow structure.** Brand edits only touch `responses` and approved `TenantConfig` fields.
2. **Overrides are keyed by `reply_id`**, matching `app/flows/*.yml` and `COLLECT_SLOT_REPLY_IDS` in `nlg.py`.
3. **Every brand line passes the same gate** as platform defaults — publish-time validation is advisory; runtime gate is authoritative.
4. **Draft / publish separation** — live calls never read draft rows.

---

## Multi-tenant data model (Supabase)

Proposed tables (names illustrative; migrate via Fonada Supabase repo):

### `collections_dialogue_packs`

One logical pack per agent (recommended) or per organization (alternative — see [decisions](#product-decisions-recommended-defaults)).

| Column | Type | Notes |
|--------|------|-------|
| `id` | uuid PK | |
| `organization_id` | uuid FK → `organizations` | RLS scoped |
| `agent_id` | uuid FK → `agent_configs` NULL | NULL = org-wide default pack |
| `name` | text | e.g. "HDFC collections v2" |
| `status` | enum | `draft` \| `in_review` \| `published` \| `archived` |
| `platform_flow_version` | text | Git tag / semver of Collection flows this pack was validated against |
| `created_by` | uuid | |
| `published_at` | timestamptz | |
| `published_by` | uuid | |

### `collections_response_overrides`

| Column | Type | Notes |
|--------|------|-------|
| `id` | uuid PK | |
| `pack_id` | uuid FK | |
| `reply_id` | text | Must exist in platform catalog |
| `language` | text | `hi` \| `en` \| `hinglish` |
| `tone_register` | text NULL | `standard` \| `de_escalate` \| `reassure` \| … |
| `text` | text | Template with `{slot}` placeholders |
| `channel` | text NULL | `voice` \| `whatsapp` \| NULL = both |
| `is_enabled` | bool | Allow soft-disable of optional lines |

Unique constraint: `(pack_id, reply_id, language, tone_register, channel)`.

### `collections_collect_prompt_overrides`

Overrides for `TenantConfig.collect_slot_prompts` (slot name → prompt text).

| Column | Type |
|--------|------|
| `pack_id` | uuid FK |
| `slot_name` | text | e.g. `ptp_date`, `identity_response` |
| `language` | text |
| `text` | text |

### `collections_agent_bindings`

Links a telephony agent to its active published pack.

| Column | Type |
|--------|------|
| `agent_id` | uuid FK → `agent_configs` UNIQUE |
| `published_pack_id` | uuid FK |
| `tenant_id` | text | Resolved ID sent to `/turn` (may mirror `organizations.slug`) |

### `agent_configs` extension

Add column (migration):

```sql
ALTER TABLE agent_configs
  ADD COLUMN IF NOT EXISTS agent_mode text DEFAULT 'llm';
  -- new value: 'collections_engine'
  ADD COLUMN IF NOT EXISTS collections_pack_id uuid REFERENCES collections_dialogue_packs(id);
```

Agents with `agent_mode = 'collections_engine'` use the Collections brain instead of LLM/Flow Builder paths.

### RLS

- Users read/write packs where `organization_id` matches their `user_profiles.organization_id`.
- Published packs are readable by edge service role for runtime cache warm.
- Platform admins bypass for compliance review queue.

---

## Brain runtime — loading and merging overrides

### New module: `app/config/dialogue_loader.py` (proposed)

```python
def load_effective_flows(
    tenant_id: str,
    agent_id: str | None = None,
    *,
    supabase_client=None,
) -> FlowSet:
    base = load_all_flows()
    overrides = fetch_published_overrides(tenant_id, agent_id)
    return merge_response_overrides(base, overrides)
```

### Merge rules

1. Start with platform `FlowSet` from disk (immutable in prod container).
2. Fetch **published** pack for `(tenant_id, agent_id)`; fall back to org default pack.
3. For each override row: **replace** matching variant in `flows.responses[reply_id]`, or **append** if new language/tone combo.
4. Never delete platform response keys — if brand disables a line, engine falls back to platform default (`is_enabled = false` skips override).
5. Apply `collect_prompt_overrides` into a `TenantConfig` copy before NLG collect paths run.
6. Cache merged result in Redis keyed by `(tenant_id, agent_id, pack_published_at)` with TTL ~5 min; invalidate on publish webhook.

### Changes to existing code (minimal)

| File | Change |
|------|--------|
| `app/engine/turn.py` | Pass merged `FlowSet` + per-tenant `TenantConfig` into executor/NLG |
| `app/config.py` | `tenant_config(tenant_id, *, pack=...)` merges collect prompts |
| `app/schemas/api.py` | Add optional `agent_id` to `TurnRequest` |
| `app/main.py` | Wire Supabase client / cache on startup |

### `reply_id` catalog

Brands may only override IDs present in the platform catalog. Expose a read-only manifest (JSON) generated in CI from `app/flows/*.yml`:

```json
{
  "ask_identity_verification": {
    "flow": "identity_verification",
    "category": "identity",
    "slots": [],
    "languages": ["hi", "en", "hinglish"],
    "compliance_tags": ["identity", "no_debt_before_verify"]
  }
}
```

The dashboard uses this manifest to render grouped editors (Identity, Dispute, PTP, Compliance handoff, …).

---

## Publish pipeline (draft → validate → compliance → live)

```
  Brand author          Automated checks              Compliance reviewer (optional)
      │                        │                                │
      ▼                        ▼                                ▼
   [draft] ──save──►  schema validate  ──pass──►  [in_review] ──approve──► [published]
                           │                                              │
                           ├─ unknown reply_id → reject                   └─► cache invalidate
                           ├─ unknown {slot} → reject                         webhook → brain
                           ├─ platform phrase scan (warn/block)
                           ├─ dry-run gate() with fixture states
                           └─ diff vs platform default (audit log)
```

### Validation (edge function `collections_dialogue_validate`)

1. **Schema:** reply_id in catalog; `{slot}` names allowed for that template.
2. **Placeholder safety:** no `{amount}` before identity verified in identity-category templates (mirrors `template_references_debt` rules).
3. **Phrase policy:** scan against `prohibited_outbound_phrases` and `collection_pressure_phrases` — block on match.
4. **Gate dry-run:** run `gate(text, fixture_state, tenant_cfg)` for representative states (pre-verify, vulnerable, opt-out, dispute_hold).
5. **Completeness (warn):** missing overrides for required reply_ids when agent enables certain flows.

### Publish

- Sets `status = published`, `published_at = now()`, archives prior published pack for same agent.
- Updates `collections_agent_bindings`.
- Emits event for brain cache invalidation.

Failed runtime gate (should be rare if publish validation is good) still replaces line with `safe_fallback_reply` — same as today.

---

## Frontend module (AI-platform integration)

### Placement

Add a **new sidebar entry** in `Sidebar.tsx`:

```typescript
// src/components/Dashboard/Sidebar.tsx
{ icon: MessageCircle, label: 'Collections Dialogue', path: '/dashboard/collections-dialogue' },
```

Register route in `App.tsx`:

```typescript
<Route path="collections-dialogue" element={<CollectionsDialogue />} />
<Route path="collections-dialogue/:agentId" element={<CollectionsDialogueEditor />} />
```

Show tab only when:

- User's org has `collections_engine` feature flag, **or**
- Selected agent has `agent_mode === 'collections_engine'`.

### Proposed folder structure (inside AI-platform)

Keep Collections UI isolated for reviewability; no changes to Agent Builder LLM forms.

```
src/
  features/
    collections-dialogue/
      index.ts                      # public exports
      pages/
        CollectionsDialogue.tsx     # agent list + pack status
        CollectionsDialogueEditor.tsx # main editor shell
      components/
        PackStatusBadge.tsx
        ReplyIdGroupAccordion.tsx   # Identity, Dispute, PTP, …
        ResponseVariantEditor.tsx   # language tabs, tone, template text
        SlotPromptEditor.tsx
        PublishReviewPanel.tsx
        CompliancePreview.tsx       # dry-run gate results
      hooks/
        useCollectionsPack.ts
        useReplyIdCatalog.ts
      services/
        collectionsDialogueService.ts  # Supabase CRUD + validate/publish edge calls
      types/
        dialoguePack.ts
```

### Editor UX (v1)

1. **Select agent** — filter `agent_configs` where `agent_mode = 'collections_engine'` (or channel = telephony + category = collections).
2. **Pack overview** — draft vs published, last published date, validation status.
3. **Grouped response editor** — accordion per flow category; each `reply_id` shows platform default (read-only) + brand override fields.
4. **Language tabs** — hi / en / hinglish side by side.
5. **Live preview** — interpolate sample slots; show gate verdict badge (calls validate edge function).
6. **Publish** — disabled until validation passes; confirm diff summary.

### Coexistence with Agent Builder

| Agent mode | Configuration surface |
|------------|----------------------|
| `llm` | Existing BasicInfo: `system_prompt`, `first_message` |
| `flow` | Flow Builder → `conversation_flows` |
| `collections_engine` | **Collections Dialogue** tab + minimal BasicInfo (voice, language, phone number) |

Do **not** map `first_message` directly to free text for collections agents — map it to **`call_opening`** `reply_id` override (or a dedicated opening step) so the gate sees the same template path.

### Service pattern

Follow existing Supabase patterns (`secureAgentInfoService`, `whatsappUnifiedService`):

```typescript
// collectionsDialogueService.ts
export async function publishPack(packId: string) {
  const { data, error } = await supabase.functions.invoke('collections_dialogue_publish', {
    body: { pack_id: packId },
  });
  if (error) throw error;
  return data;
}
```

---

## Worked example — identity verification copy

### Platform default (`app/flows/identity_verification.yml`)

```yaml
responses:
  ask_identity_verification:
    - text: "[COMPLIANCE-REVIEW] Security ke liye pehle aapki pehchaan verify karni hogi..."
      language: hi
```

Flow reference:

```yaml
steps:
  - collect: identity_response   # uses ask_identity_verification via COLLECT_SLOT_REPLY_IDS
```

### Brand override (Supabase row)

| reply_id | language | text |
|----------|----------|------|
| `ask_identity_verification` | hi | `{lender_name} ke liye call kar rahe hain. Pehchaan verify karne ke liye apni janam tithi batayein.` |

### Runtime

1. Telephony sends `POST /turn` with `tenant_id`, `agent_id`, first turn `transcript=""` or opening trigger in `turn_meta`.
2. Brain loads merged flows; identity flow runs first (priority `identity`).
3. NLG renders `ask_identity_verification` with brand text; `{lender_name}` filled from hydrated slots.
4. Gate checks: no debt/arrears disclosure pre-verify (`reply_discloses_debt_or_arrears`); no prohibited phrases.
5. TTS speaks final gated text.

### Opening call scenario

Define a platform `reply_id` e.g. `call_opening` (or reuse `voice_first_message` mapping):

- **Platform default** in YAML.
- **Brand override** in dashboard → stored in `collections_response_overrides`.
- Telephony `turn_meta.opening = true` causes executor to utter `call_opening` before first collect.

---

## Telephony / edge wiring

Today Go telephony + Supabase edge functions route to LLM agents. For collections:

```
Inbound call
    → edge middleware resolves agent_configs row
    → if agent_mode == 'collections_engine':
           map organization → tenant_id
           POST {COLLECTIONS_BRAIN_URL}/turn
                { call_id, tenant_id, agent_id, borrower_id, transcript, locale, turn_meta }
           ← { reply_text, end_call, transfer_to_human, disposition }
    → TTS + next ASR segment
```

Environment:

- `COLLECTIONS_BRAIN_URL` — Cloud Run / internal URL of this service.
- Service role reads `collections_agent_bindings` + published overrides (or brain loads directly).

**IDs:** Pass both `organizations.id` (billing/features) and a stable `tenant_id` string the brain uses for config lookup. Recommended: `tenant_id = organizations.slug` or explicit `organizations.collections_tenant_id` column.

---

## Product decisions (recommended defaults)

| # | Question | Recommendation |
|---|----------|----------------|
| 1 | New `agent_mode` vs reuse `flow`? | **`collections_engine`** — explicit routing in telephony; avoids collision with React Flow Builder. |
| 2 | Overrides per agent vs org? | **Per agent**, with optional org-level default pack inherited when agent has no published pack. |
| 3 | Map `first_message` to brain? | **Yes — indirect:** UI label "Opening message" writes to `call_opening` reply_id override, not LLM `first_message`. |
| 4 | Coexist with Flow Builder? | **Yes.** Collections tab only for `collections_engine` agents; Flow Builder unchanged. |
| 5 | New Supabase tables? | **Yes** — tables above; no borrower PII in dialogue tables. |
| 6 | Brain API IDs? | **`tenant_id` + `agent_id`** on `/turn`; resolve pack via `collections_agent_bindings`. |

---

## Implementation phases

### Phase A — Platform manifest + brain merge (Collection repo)

- [ ] Generate `reply_id` catalog artifact in CI.
- [ ] Implement `merge_response_overrides()` + tests.
- [ ] Add `agent_id` to `TurnRequest`.
- [ ] Supabase loader stub + in-memory fixture for local dev.

### Phase B — Supabase schema + edge validate/publish

- [ ] Migrations for pack/override/binding tables.
- [ ] RLS policies.
- [ ] `collections_dialogue_validate` and `collections_dialogue_publish` edge functions.

### Phase C — Dashboard module (AI-platform)

- [ ] Sidebar + routes.
- [ ] Agent list filter + editor components.
- [ ] Wire validate/publish; show platform defaults alongside overrides.

### Phase D — Telephony integration

- [ ] `agent_mode = collections_engine` routing in Go/edge.
- [ ] Cache invalidation on publish.
- [ ] End-to-end pilot with one org.

---

## Open questions

1. **Compliance review queue:** Is publish self-service for all brands, or does Fonada compliance team approve `in_review` → `published`?
2. **Flow palette:** Can brands disable entire flows (e.g. hardship), or only customize copy within enabled flows?
3. **Version pinning:** When platform ships new `reply_id` keys, do unpublished packs auto-gain platform defaults or block publish until reviewed?
4. **`tenant_id` cleanup:** Most `user_profiles.tenant_id` values are `"DefaultTenant"` — migration plan to org-scoped IDs?
5. **WhatsApp channel:** Same override tables with `channel = whatsapp`, or separate shorter templates?

---

## Summary

Brands control **templated words** keyed by **`reply_id`**, edited in a new **Collections Dialogue** dashboard tab, stored in **Supabase**, and merged at runtime into the existing **`render()` → `gate()`** pipeline. Flow logic, actions, and compliance remain platform-owned. The Collection repo change is a thin override merge layer; the heavy UX lives in **AI-platform** as an isolated feature module under `src/features/collections-dialogue/`.
