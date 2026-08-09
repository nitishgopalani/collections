# FONADA VOICE COLLECTIONS — MASTER SPRINT PLAN
_v2.0 · 09 Aug 2026 · git-verified against brain `810647d` (feature/tier23-engine-upgrade), go-server `4e52063` (release/uat-voice-stack), connector `0b7a252` (feature/async-ws-audio-writes), orchestrator `2408ce2`_
_Supersedes: W2_SPRINT_SPEC v1 (now v2, updated alongside) · Architecture doc: v1.3-final ADOPTED (turn-flow PDF merged with 4 amendments)_

---

## SECTION 1 — DONE (verified in git today, not from memory)

### 1A. Conversation engine (brain)
| Item | Evidence |
|---|---|
| Tier-1 deterministic coercions, profile-driven + INABILITY_RE + reason_catchall | `96c5719`, guards `refusal_matched_via` |
| Tier-2 full-catalog routing, RAG demoted, kill-switch | CP2, live p50 +60ms |
| Tier-3 respond + respond_guard + gate-on-combined + blank-transcript strip | `cfc5ed2`, live-proven fb6a0f02 t4/t5 |
| Attempt-indexed escalation (`ResponseTemplate.attempt`, `escalate_to`) | P4 |
| Compliance gate: Hindi legal/NPA lexicon, "emi" token removed, tenant allowlist (8 phrases, `allowlisted:true` logged) | P5.4/F1 |
| Tenant truth: `client_id > session_tenant_id`, no TEST_MODE pin, `TEST_FORCE_TENANT` explicit-only | `bc818c6` |
| Hydration single-source: DB-wins, fixture only on explicit env, sentinel `"unknown"` query-proof, DATE→iso | `c88e691`, `2d35999`, `9f29ccf` |
| PaisaLo loan fields migration + PREDUE seed (Ramesh, dpd=-5, branch grounded) | `d10e8f8` |
| Opener resilience: exception → deterministic template greeting (`OPENER_FALLBACK`) | `42e68da` F1 |
| Zombie-turn fix (`frustration_escalate` in end_call OR) + `agent_fault` repair guard | `42e68da` F3 |
| Per-scenario greetings, voice set at opener step 1, B2 offer chunking, B1 short re-ask, B3 holding 5s | `c88e691`, `a038528` |

### 1B. Voice / media (go-server + connector)
Deferred WS open with first-Speak voice (`a1bdeff`) · hold-turn inherits parent + session lastVoice (`ce583f3`, `852c792`) · serialized TTS cache (one producer/turn) · monotonic egress watermark + barge clear-frame + connector toAst drain · finalize-after-first-audio · REST fallback on ws error + v2 speaker remap + pace clamp · 8k end-to-end rate pinning + mismatch WARN · client_id forwarded verbatim (`4e52063`) · connector `client_id_source=metadata>listener>env` + phones from media-meta (`0b7a252`) · `/version` on go-server+connector.

### 1C. Live-verified behaviours (real calls)
d501f625: full PREDUE health (phone→Ramesh→predue→priya, zero voice-change). fb6a0f02: Tier-3 grounded answers ×2 + resume, refusal via regex. H1 silent smoke f12e1caa: `source=client_id`, TEST_MODE=false.

### 1D. NOT in repo (chat-only artifacts — commit these)
`PIPELINE_AUDIT.md` (404 in docs/), `CURSOR_HANDOVER.md` (404), architecture HTML v1.3, Fragment Library v1, this plan. → W1-A housekeeping item.

---

## SECTION 2 — PENDING, git-proven

Verified gaps at HEAD `810647d`: paisalo `willing` pack = **10 entries** (no theek hai/haan), `willing_disqualifiers` = **absent**, coercion_chain = `dispute→willing→refusal→identity→reason_catchall` (**no reversal stage**; `reversal_slots:[]` empty), H2 dead-air paths absent, policy interrupts absent, compose/gate/evidence-scorer absent, PTP absent, dialer layer un-audited.

| Sprint | Scope | Effort | Status |
|---|---|---|---|
| **W1** | PLO-OOF + H2 dead-air + policy interrupts | ~5d | **GREENLIT, NOT STARTED** (prompt issued) |
| **W2 v2** | Evidence scorer + Commitment Gate + Compose + v1.3-final contracts | ~6.5d + shadow week | Spec ready (updated today) |
| **W3** | PTP engine · computed slots · call-history+mid-call re-hydrate · post-call obligation loop · inbound DID · LLM-429 degrade · multi-loan rule · persist-async (P1 gap) | ~6d | Scoped |
| **W4** | Dialer audit+controls (DNC/cadence/dedup) · graceful drain · Websocket CI · per-call summary line · brain+orch /version · mining script · secret rotation | ~4d | Scoped |
| **Debt (rolling)** | G-B6-01/02/03 test fixes (in W1-A) · 29 test-order pollution · turn.py SOT aliases · lab-path slin16 (G-A3-02) | — | Register |

**Total ≈ 21.5 Cursor-days ≈ 4 working weeks. Pilot gate = W1 + W2-enforce ≈ 2–2.5 weeks.** W3/W4 run during pilot except: DNC suppression + graceful-drain rule + obligation-loop export must land **before client-facing scale-up** (pilot-internal calls OK without).

---

## SECTION 3 — SPRINT DEFINITIONS

### W1 (unchanged from greenlit prompt) — checkpoints W1-A/B/C
A: cue packs (full willing/disqualifiers/callback/**reversal stage+slots**), re-ask ladder, fb6a0f02-t5 grounding forensic, B-side quick wins (G-B6-01/02, G-B3-01, G-B4-01/02/03), replay t6-"ठीक है"-advances. B: H2 (asr_dead apology, TTS-fail×2 close, reply_empty log, loud env validation). C: policy interrupts (vulnerability verify+wire, DNC capture, window close-out, third-party/speaker-flip lock). **+A6 housekeeping: commit §1D docs into docs/.**

### W2 v2 (see W2_SPRINT_SPEC.md v2 — amended today)
Adds over v1: **router contract** (oof_class+subclass incl. prompt_injection/repeated_diversion, SAME LLM call — no second hop; OOF fields omitted on normal turns) · **UNRELATED deterministic lane** (world-knowledge/RAG/tools/Tier-3 OFF; scope-boundary fragment + canonical re-ask ALWAYS — "answer" never means content) · **canonical resume state** (full/short/binary variants; never replay TTS buffer) · **source-trust hierarchy** (system>hydrated>KB>fragment>borrower_claim>LLM; fail-closed on stale account_fact; pilot: session-snapshot=fresh) · **repeated-diversion ladder with its OWN counter** (separate from repair) · **A1: LLM confidence = telemetry-only, never a gate input** · fragments 51→**54** (irrelevant_redirect + 2 identity-safe scope templates).

### W3 — Commitments & context (~6d)
PTP policy engine (client grace rules needed) 1.5d · computed slots 0.5d · call-history hydration + mid-call re-hydrate + payment-lag 1d · **post-call obligation loop** (dispositions→client export, callback re-queue, flagged worklist) 1.5d · inbound DID greeting 0.5d · LLM-429 deterministic-degrade 0.5d · multi-loan rule 0.25d · persist async (Upstash off critical path — closes the F1 root cause properly) 0.25d.

### W4 — Dialer & platform (~4d)
Dialer audit 0.5d + DNC suppression/cadence/dedup fixes 1.5d · graceful drain 0.5d · Websocket CI 0.5d · per-call summary log line 0.25d · brain+orchestrator /version 0.5d · weekly mining script 0.25d · media-secret rotation (ops).

---

## SECTION 4 — AMENDMENTS MADE TODAY (confirmation)
1. **W2_SPRINT_SPEC.md → v2**: v1.3-final contracts merged (list in §3-W2). v1 wording "51 fragments" superseded by 54; unrelated lane added to W2-3; diversion ladder added as W2-3b; gate section gains source-trust table + confidence-telemetry rule.
2. **Fragment Library v1 → v1.1 note**: +3 (irrelevant_redirect, scope_boundary_pre_identity, scope_boundary_post_identity) = **54 + unknown_info**. Full texts to be drafted at W2-3 from the PDF's two templates.
3. **Architecture HTML**: header already v1.3 FROZEN; treat uploaded turn-flow PDF as the canonical per-turn diagram — **ADOPTED with 4 amendments** (A1 confidence-telemetry-only · A2 router=same-call · A3 unrelated-"answer"=boundary-only · A4 separate diversion counter).
4. **Effort totals**: 17-18d → **~21.5d** (obligation loop, DID, drain, +router work absorbed).

## SECTION 5 — DECISIONS REGISTER (all open items, one place)
| # | Decision | Owner | Blocks |
|---|---|---|---|
| N-1 | MFI in pilot scope? (yes = +1d product-gated copy in W1/W2) | Nitish | W1-A copy, W2-3 |
| N-2 | Media secret rotation timing | Nitish | W4 (pre client-facing) |
| D-P1..P8 | Fragment decisions (penalty/UPI-receipt-SMS/interest/recovery-agent/AI-disclosure/number-source/recording-revoke/language) | Nitish+client | W2-3 |
| C-1 | PaisaLo WRITTEN approval: 12-item copy + Fragment Library + 8 data-feed fields | Client | W2-3, pilot |
| C-2 | Multi-loan dialing model (per-loan rows vs highest-DPD) | Client | W3 |
| C-3 | PTP grace rules (max days, min partial) | Client | W3 |
| C-4 | Inbound DID expectation | Client | W3 |
| ✅ | TEST_MODE off · voices · D-1 allowlist · D-5 pay-later · D-2/D-3 | — | closed |

## SECTION 6 — PILOT GATE (measured, not argued)
Replay: 5-round fixture set + ASR-noise variants → zero unbounded outcomes, all 7-outcome classified. Live (2 calls: on-script + deliberately messy): opener speaks t1 · zero voice-change · gate `would_downgrade` sane in shadow → enforce clean · `escape_hatch_used <5%` · `ESCALATED_UNCLEAR=0` on info turns · confirm-per-call ≤1.5 · oof_class logged on every OOF turn · gate_warnings only allowlisted on postdue3/NPA. Ops: no deploys in dialing hours until drain lands; every deploy /version-verified.
