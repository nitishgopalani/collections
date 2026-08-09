# CURSOR BOOT — v1.3-final Implementation (Fresh Session)
_Paste this as the FIRST message in the new Cursor window, along with: MASTER_SPRINT_PLAN.md, W2_SPRINT_SPEC.md, PAISALO_FRAGMENT_LIBRARY_V1.md, CURSOR_HANDOVER.md, VOICE_STACK_ARCHITECTURE.html, and the adopted Turn-Decision-Flow PDF. Claude (chat) is the reviewing architect; Nitish signs off. 09 Aug 2026._

## Standing rules (unchanged, non-negotiable)
NO commit/deploy/live-call without sign-off · every claim carries evidence (log line, test name, stash-proof) · clean builds only, /version verified, no docker-cp, no sed-on-.env · found a bug outside scope → RECORD in tracker, never fix silently · dead air is the worst failure · STOP at every ✋ checkpoint.

## Repos & deployed truth (verify before touching)
brain `collections@feature/tier23-engine-upgrade` HEAD 810647d · go-server `websocket@release/uat-voice-stack` HEAD 4e52063 · connector `asterisk-connector@feature/async-ws-audio-writes` 0b7a252 · orchestrator 2408ce2. TEST_MODE=false on UAT. Focus tenant: **PaisaLo** (SOT here is a test harness only; production SOT ships from a different codebase).

---

## PHASE 0 — REPO CONTEXT DOCUMENT (before ANY audit or code)
Scan the entire brain repo slowly and fully — every file under app/ (engine, flows, tenants, memory, ws, schemas, clients), migrations, tests, scripts — plus the go-server internal/media+brain and connector internal/. Produce **`docs/REPO_CONTEXT.md`** committed to the brain repo:
- Module map: every file, one line — what it does, key entry points, who calls it.
- Turn lifecycle walk: session_start → tenant resolve → hydration → coercions → command_gen → executor → NLG → gate → chunk, with file:function refs at each hop.
- Tenant-conditional inventory (feeds AUDIT-2): EVERY hardcoded `"salary_on_time"` / `"paisalo"` / `sot_` / `plo_` conditional in engine code (turn.py has 32, nlg.py 30, config.py 5 — find them ALL), each with file:line, what it special-cases, and whether a TenantRuntimeProfile field could replace it.
- Config/env inventory (refresh of audit A2 at current HEAD).
- Data model: borrowers schema (002 migration fields), media_streams, slots taxonomy, FACT_SLOTS_FOR_RESPOND.
- Known-debt appendix: import from tracker (G-B6-01/02/03, 29 pollution tests, SOT aliases, G-A3-02, persist-sync P1 gap).
This document is your permanent context anchor — update it at every checkpoint so a future session never re-discovers the codebase.
✋ CHECKPOINT 0: commit docs/REPO_CONTEXT.md + the five chat-only docs (MASTER_SPRINT_PLAN, W2_SPRINT_SPEC, PAISALO_FRAGMENT_LIBRARY_V1, CURSOR_HANDOVER, PIPELINE_AUDIT if retrievable) into docs/. STOP.

## PHASE A2 — AUDIT-2: MULTI-TENANCY (audit only, no fixes)
Motivation (live evidence): SOT fixture leaked into a PaisaLo call (Rishabh borrower), sot_test_mode blocked the PaisaLo DB lookup (fixed by exclusion patch 0656a31 — itself another hardcode). The pattern: post-P1 hotfixes re-introduced string-compare tenancy.
Deliver **`docs/TENANCY_AUDIT.md`** + Gap Register rows [ID | file:line | conditional | risk | proposed profile field | effort S/M/L]:
1. Every tenant conditional from the Phase-0 inventory, classified: (a) legitimately profile-driven already, (b) should be a profile field, (c) SOT-only test-harness shim (mark for quarantine behind one flag), (d) dangerous cross-tenant leak path.
2. Leak-path analysis: trace how each of the three live mixing incidents happened (Rishabh-in-PaisaLo, sot_test_mode block, sot_greeting fallback references) and whether the class of bug is now structurally closed or just patched.
3. NLG/flows namespace check: can a plo_ call ever render a sot_ reply id (fallbacks, clarify maps, force_flow, error paths)? List every such path.
4. config.py tenant_config special-cases vs profile — migration plan sketch.
5. Recommendation: DETENANT refactor scope (make it W1-A0 if ≤1.5d, else its own mini-sprint before W1).
✋ CHECKPOINT A2: audit doc + register. Claude will rule on DETENANT scope. STOP — no fixes yet.

## PHASE ORDER AFTER A2
DETENANT (as ruled) → W1-A/B/C (existing greenlit spec in MASTER_SPRINT_PLAN §3 — PLO-OOF cue packs, H2 dead-air, policy interrupts) → W2 v2 (evidence scorer, Commitment Gate shadow→enforce, compose+54 fragments, router contract, unrelated lane, diversion ladder) → W3 → W4. Tracker: docs/IMPLEMENTATION_TRACKER_V2.md governs status; update it at every checkpoint, commit with the checkpoint.

## Live-call protocol
Any live redial: silent smoke first (Local/5000 style), then announce "ready", Nitish answers ONE call, dump against written pass-criteria, STOP.
