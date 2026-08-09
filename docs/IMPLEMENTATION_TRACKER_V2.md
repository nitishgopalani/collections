# IMPLEMENTATION TRACKER V2 — Fonada Voice Collections

_Phase map + progress bars. Maintained by the implementer._
_This file is the on-disk anchor; the chat-only `Collection/IMPLEMENTATION_TRACKER_V2.md` (0-byte on disk at scan) holds the fuller phase narrative in the IDE buffer and must be merged in by the user._

> **NOTE (P0 audit, 09 Aug 2026):** The chat-only `IMPLEMENTATION_TRACKER_V2.md` at the repo root is 0-byte on disk; its content lives only in the IDE editor buffer. This on-disk copy in `docs/` is the canonical anchor going forward. The user should paste the IDE-buffer content into this file and re-save, then `git add` it.

---

## PHASE MAP

| Phase | Status | Bar | Notes |
|---|---|---|---|
| **P0 — Repo Context Audit** | [R] | 100% | `docs/REPO_CONTEXT.md` written. CP0 commit pending architect sign-off. |
| A2 — Config Deprecation | [ ] | 0% | Move 6 `sot_*` Settings fields into `TenantRuntimeProfile` (DEBT-003). Blocked on P0 sign-off. |
| DT — Decision-Tree Hardening | [ ] | 0% | — |
| W1-A — SOT/PLO On-Rails Hardening | [~] | 60% | P1-P5 landed in `958135d` (PLO-OOF). Confirm scope with architect. |
| W1-B — Identity Gate Hardening | [ ] | 0% | — |
| W1-C — Compliance Gate Hardening | [ ] | 0% | — |
| W2-1 — Evidence Scorer + Echo Filter | [ ] | 0% | — |
| W2-2 — Commitment Gate | [ ] | 0% | — |
| W2-3 — Compose Lane + Fragment Library | [ ] | 0% | — |
| W2-4 — Enforce + Replay Regression | [ ] | 0% | — |
| W3 — Tenant Isolation | [ ] | 0% | Per-tenant `TenantRuntimeProfile` field migration (DEBT-001..010). |
| W4 — Multi-Tenant Scale | [ ] | 0% | Per-tenant borrower tables (DEBT-011), script triage (DEBT-013). |

---

## CHECKPOINT LOG

### CP0 — 09 Aug 2026 — P0 Repo Context Audit
- **Status:** [R] (ready for architect sign-off).
- **Deliverable:** `docs/REPO_CONTEXT.md` (module map, turn lifecycle walk, complete tenant-conditional inventory, config/env inventory, data model, known-debt appendix).
- **Tenant-conditional counts (regex `sot_|plo_|salary_on_time|paisalo`):**
  - `app/engine/turn.py`: 78 matches (boot hint ~32; ~12 real branch points, rest are local var names / data).
  - `app/engine/nlg.py`: 30 matches (matches hint; all data — slot->reply_id dict entries).
  - `app/config.py`: 27 matches (boot hint ~5; 1 real branch at line 610, rest are `Settings` field defaults).
  - `app/engine/actions.py`: 70+ matches (action-name dispatch + slot keys — data, not branches).
  - `app/engine/scripted_coercions.py`: 7 matches (2 real branches on `profile.flow_prefix`).
  - `app/engine/label_transition.py`: 14 matches (1 real branch at line 121, rest are SOT label maps).
  - `app/engine/identity_gate.py`: 2 matches (1 hardcoded `sot_opener` flow name).
  - `app/ws/handler.py`: 9 matches (3 real branches on `is_paisalo_test` / `test_force`).
  - `app/ws/routing.py`: 50+ matches (`FORCE_FLOW_ALIASES` + `ALLOWED_OPENERS` data).
  - `app/memory/test_borrower.py`: 12 matches (2 real branches on `state.tenant_id == "paisalo"`).
- **Real branch points total: 12.** All 12 are profile-able with new `TenantRuntimeProfile` fields (mostly bools or lists). See `docs/REPO_CONTEXT.md` §3.
- **Surprises found:**
  1. Brain HEAD drift: actual `958135d` vs boot-doc `810647d`. PLO-OOF (W1-A P1-P5) already landed.
  2. Chat-only docs are 0-byte on disk; content lives in IDE buffer only.
  3. `CURSOR_HANDOVER.md` is missing entirely (never created). The "8 live-bug classes" are not in the repo.
  4. `docs/PIPELINE_AUDIT.md` already exists (untracked, 28293 B).
  5. 8 modified golden transcripts + ~180 untracked scratch scripts in brain working tree (not touched by this audit).
- **Rules honored:** Zero code changes, zero fixes, zero deploys. Audit/documentation only.
- **Stop:** Do NOT start Phase A2 until the architect signs off.

---

## HARD INVARIANTS (carried from boot doc)

1. **GATE-BEFORE-SIDE-EFFECT.** No warm-transfer, WhatsApp send, or call close before the compliance gate has returned `verdict=allow`.
2. **POLICY PRECEDENCE.** Tenant policy > compliance defaults > engine defaults. Never the reverse.
3. **NO SILENT REWRITES.** Every coercion / LTL rewrite must be logged to the turn decision log.
4. **STATE IS THE SOURCE OF TRUTH.** Borrower record is hydrated into `ConversationState` at session_start; the engine never reads the DB mid-turn.
5. **TEST MODE IS OFF IN UAT.** `TEST_MODE=false` on UAT. Test-mode branches are dev-only.
6. **NO TENANT STRING IN ENGINE.** No `if tenant_id == "..."` in `app/engine/` (W3 target). Currently 12 violations — see `docs/REPO_CONTEXT.md` §3.

---

## KNOWN DEBT REGISTER

See `docs/REPO_CONTEXT.md` §6 for the full 15-item debt register (DEBT-001..DEBT-015). Summary by phase:
- **W2-A2:** DEBT-001..005, 007, 008, 010, 012 (config + branch deprecation).
- **W3:** DEBT-006, 009 (per-tenant isolation).
- **W4:** DEBT-011, 013 (multi-tenant scale + script triage).
- **P0 (user):** DEBT-014, 015 (golden re-record + chat-only doc save).
