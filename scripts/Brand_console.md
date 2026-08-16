# BRAND CONSOLE v0 — SPEC
_v1.0 · 15 Aug 2026 · Testing-only: NO auth (env-gated), separate frontend project, handoff to platform team later · Base: brain 33821c0_

## Ground rules
- Frontend = NEW folder/repo `fonada-console` (Vite + React + Tailwind). No login, no auth. Aurora 2.0 tokens: canvas #0B1210, panel #0F1713, accent #10B981, text #E7EFE9, mono for ids.
- Backend = `/admin/v0` router INSIDE brain (FastAPI). `ADMIN_API_ENABLED=false` default; enable only on UAT internal interface; CORS: http://localhost:5173 only. NEVER exposed publicly. Every write logged (admin_audit.jsonl: ts, endpoint, before/after hash).
- All writes go through existing validation + hot-reload paths. No new persistence — the tenant YAMLs ARE the database.

## UI-0 — Admin API (1d) ✋ CP-UI0
GET  /admin/v0/tenants                      → list
GET  /admin/v0/tenant/{id}/profile          → editable profile fields (voices/pace per scenario, dpdp_*, call window, ptp policy, ladder counts, backchannel/cue pack readonly view)
PUT  /admin/v0/tenant/{id}/profile          → validate → write YAML → reload → {ok|errors[]}
GET  /admin/v0/tenant/{id}/fragments        → library with tags/variants
PUT  /admin/v0/tenant/{id}/fragment/{fid}   → validate → write → reload
POST /admin/v0/tenant/{id}/compliance-dry-run → P5.0 gate over library/changed set → per-line pass/fail/allowlisted
POST /admin/v0/tts-preview {text, voice_id, pace} → audio/wav bytes (Sarvam REST path)
POST /admin/v0/tenant/{id}/test-turn {session_id?, transcript} → {reply_text, guards: {evidence, gate_verdict, oof_class, fragment_ids, disposition, llm_call_reason}} — wraps the existing test harness; maintains an in-memory session
GET  /admin/v0/exports?date=YYYYMMDD&kind=dispositions|callbacks|worklist → rows
Tests: profile PUT invalid → 422 with field errors; dry-run flags a prohibited line; test-turn returns guards for a willing turn.

## UI-1 — Brand Console page (1.5d) ✋ CP-UI1
- Tenant picker (top bar). Scenario cards (predue/ondue/postdue1-3/npa): voice dropdown (catalog list) + ▶ preview button (plays tts-preview of that scenario's greeting line), pace slider 0.5-2.0.
- Toggles/knobs: dpdp_third_party_lock (strict/relaxed), dpdp_disclosure_tier_enforced, call window (start/end/timezone), max_ptp_days, min_partial_pct, counter_max_attempts, ladder counts.
- Save bar: Validate → Save & Reload; inline field errors; unsaved-changes guard. Read-only chips for cue-pack sizes ("willing: 54 cues").

## UI-3 — Test Console page (1d, BEFORE UI-2 — demo priority) ✋ CP-UI3
- Chat pane: type a borrower line → bot reply bubble. Session controls: tenant + scenario/borrower seed pick, reset.
- Guards strip under every turn: evidence (0-3 with reason), gate verdict chip (execute/downgrade/hold), oof_class/subclass, fragment ids, llm_call_reason (cue_hit/cache/called), disposition on close.
- "Copy transcript" + "Replay as fixture" (downloads a test-fixture JSON of the session).

## UI-2 — Fragment Studio (1.5d) ✋ CP-UI2
- Table: id, category, Hindi text (rendered with sample slots), answers tags, safe_in, scenario/product gates, variant count.
- Edit drawer: text + variants (formal/warm/firm), tags. Tone knob per tenant (profile field variant_tone) with live example render.
- "Compliance Check" button → dry-run results inline (pass/allowlisted/blocked per line). Blocked lines cannot be saved as active.

## UI-4 — Obligations Dashboard (0.5d) ✋ CP-UI4
- Date picker → three tabs (Dispositions / Callbacks / Worklist) → sortable table; worklist rows show the 30-word snippet; PTP rows highlight date+amount. Export CSV button.

## UI-5 — Inline copy edit (Test console) ✋ CP-UI5
GET/PUT `/admin/v0/tenant/{id}/reply/{reply_id}` (flow_utter / fragment / reask_template; system lines locked). POST `/test-turn/replay`. Console: ✏️ on every bot bubble, drawer with {slot}/{G} chips, variant tabs, TTS, compliance, Save & Reload, Replay this turn. Blocked copy 422. `admin_audit.jsonl` + "edited by console" chip.

## Sequencing
UI-0 → UI-1 → UI-3 (demo-ready console) → [W4 pilot plumbing] → UI-2 → UI-4 → UI-5. Each phase: WORKLOG entry + tracker row + sha. Frontend commits in fonada-console; API commits in brain.

## Handoff notes for platform team (write into fonada-console/README)
- Auth: wrap /admin/v0 behind platform gateway; API contract stays.
- Multi-user: add optimistic-lock (YAML hash precondition header already returned by GETs — enforce on PUT).
- The console is stateless; all state = brain YAMLs + exports files.