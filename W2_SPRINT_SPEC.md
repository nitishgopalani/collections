# SPRINT W2 v2 — Compose Lane + Commitment Gate (Hardened, v1.3-final contracts)
_Executes Turn Decision Flow v1.3-final (ADOPTED turn-flow PDF + 4 amendments). Supersedes v1. Pre-req: W1 signed off. Branch: feature/tier23-engine-upgrade. Rules: no commit/deploy without sign-off; evidence with every claim; STOP at each checkpoint._

## The five hardening invariants (from v1.3 review — non-negotiable)
1. **GATE-BEFORE-SIDE-EFFECT.** Turn pipeline = propose → gate → commit. NO slot write, PTP record, flow advance, or end_call may occur before the Commitment Gate passes the candidate. No rollback code anywhere — if you find yourself writing rollback, the ordering is wrong.
2. **POLICY PRECEDENCE.** Echo/self-talk filter → policy interrupts → evidence scorer → router. Policy detection gets NO evidence veto (vulnerability/DNC must fire even on garbled input). Echo filter runs first so our own spoken legal lines can't self-trigger the policy lane.
3. **IDENTITY/SOURCE FRESHNESS.** account_fact lane preconditioned on `identity_current=true`; speaker-flip cues decay it immediately and lock disclosure. Borrower assertions are never system facts (source tagging on every slot write: `source=system|borrower_claim|confirmed`).
4. **TIER-3 RESTRICTION.** Free-text respond is last-resort only: fires only when compose returns no viable fragments; 220-char cap; respond_guard; `escape_hatch_used` logged. Target metric <5% of OOF turns.
5. **MULTI-INTENT PRECEDENCE.** Order: vulnerability/safety > DNC > third-party/privacy > complaint > payment-assertion/PTP > call_context > related-OOF > irrelevant > unknown/repair. Commit through gate first, then OOF answer, then resume — ONE reply.
6. **CONFIDENCE IS TELEMETRY-ONLY (A1).** The router's `confidence` field is logged, never a Commitment-Gate input. The gate consumes ONLY the deterministic evidence score (0–3).
7. **ROUTER = SAME LLM CALL (A2).** oof_class/subclass/secondary_intents are extra fields on the existing command_gen output schema — no second LLM hop. OOF fields omitted on normal-flow turns (token + parse-surface discipline).
8. **UNRELATED IS DETERMINISTIC (A3).** oof_class=irrelevant → IRRELEVANT_REDIRECT: world-knowledge OFF, RAG OFF, tools OFF, Tier-3 OFF. Rendered X is ALWAYS a scope-boundary fragment (identity-safe variant) + canonical re-ask. "Answer" for unrelated never means content.
9. **TWO LADDERS, TWO COUNTERS (A4).** Repeated-diversion ladder (1st full boundary+full re-ask → 2nd short+binary → 3rd callback/graceful exit) keeps its own counter, separate from the repair counter. Curious ≠ unclear.
10. **SOURCE-TRUST HIERARCHY.** system_fact > hydrated_slot > approved_KB > fragment_default > borrower_claim > LLM_knowledge. account_fact fails CLOSED on missing/stale (pilot rule: session-snapshot = fresh). Every slot write tagged source=system|borrower_claim|confirmed.

## Phase W2-1: Evidence Scorer + Echo Filter
- Echo filter: fuzzy match transcript vs last spoken reply (normalized, threshold configurable) → drop turn, log `echo_suspected=true`, outcome=HOLD.
- Scorer (0–3): 0 = echo/backchannel token (`hmm, achha, haan?` list per tenant)/non-addressed; 1 = LLM-only; 2 = LLM+cue agree OR borrower repeated; 3 = explicitly confirmed previous turn. Score logged per turn.
- Backchannel list in tenant YAML. Tests: each score class + echo fixture from a real call log.
✋ CHECKPOINT W2-1.

## Phase W2-2: Commitment Gate (shadow first)
- Cost table in tenant YAML: script/re-ask=0 · speak-fact/neutral-slot=1 · escalate/end_call=2 · money-state slot (committed_date, offered_amount, willing-commit, already_paid claim)=3 (always one confirm-readback) · PII rows gated on identity_current.
- Gate is a pure function over (candidate_commands, evidence, cost_table) → {execute | downgrade_to_confirm(fragment_id) | hold}.
- **Executor refactor:** split into propose (build candidate) and commit (apply). Gate sits between. Grep-proof: no state mutation before gate in the turn path.
- Repair counter increments ONLY on failed confirms (generalizes agent_fault/routing_miss — replace those special cases with the one rule; keep their log fields).
- SHADOW MODE first: gate logs its verdicts (`gate_verdict`, `would_downgrade`) but does not alter behaviour. One week of UAT calls in shadow, then enforce flag.
- Tests: पच्चीस date-vs-amount fixture → readback; "ठीक है" at intent (evidence 2 via cue) → executes without confirm; borrower_claim "maine pay kar diya" → never writes system slot, routes already_paid flow.
✋ CHECKPOINT W2-2 (shadow deploy).

## Phase W2-3: Compose Lane + Fragment Library
- Load PAISALO_FRAGMENT_LIBRARY_V1 (51 fragments) into app/tenants/paisalo_fragments.yml: id, text (with {G:रही|रहा} and {slot} tokens), slots, answers[], safe_in, scenario/product gates.
- New command `compose {fragments:[id,≤2], oof_class}`; validation: ids exist, tag-compatibility (ack pair-only enforced, no threat fragments in compose), scenario/product gates, slots hydrated else swap to unknown_info.
- Renderer: fragment(s) + gender-resolve by persona voice + amounts as "X रुपये" + EXACT RESUME append (short re-ask variant).
- `oof_class` (9 values) required on every compose/respond turn; logged.
- Offline compliance pass: script gates entire library at build (like P5.0); allowlist-marked fragments verified.
- Tier-3 demotion per invariant #4.
- KB split: fragments sourced from client FAQ marked `kb_source=paisalo_faq_v1` (content ownership trail).
- complaint class → reply (ack+grievance) + `complaint_raised=true` disposition.
- **Router contract:** command_gen output gains {oof_class(9), subclass(incl. prompt_injection, repeated_diversion), secondary_intents[], confidence(telemetry)}. Canonical resume state exposed to renderer as full/short/binary re-ask variants; renderer NEVER replays the last TTS buffer — always re-renders from state.
- **UNRELATED lane (deterministic):** irrelevant → scope-boundary fragment (pre-identity variant names no loan details) + canonical re-ask; log redirect_count.
- **W2-3b Diversion ladder:** own counter; 3rd diversion → callback/graceful exit; policy interrupts always preempt; test: 2 politics questions then cooperation → no escalation, counter separate from repair.
- Fragments 51 → **54**: + irrelevant_redirect, scope_boundary_pre_identity, scope_boundary_post_identity (texts from the adopted PDF templates).
- Tests: each category one selection test; paid-vs-due tag mismatch → confirm_asked_paid; ungated fragment on wrong scenario → rejected; full replay of the 12-scenario OOF table from the break-rounds.
✋ CHECKPOINT W2-3.

## Phase W2-4: Enforce + Replay Regression
- Flip gate to enforce on UAT. Replay corpus: fb6a0f02 (all turns), 5f001c27, the ~200-turn fixture set from the five break-rounds + ASR-noise variants (systematic garbling of 20 key utterances).
- Pass bar: zero unbounded outcomes; every turn lands in one of the 7; downgrade→confirm success visible; escape_hatch <5%; redirect_count + oof_class logged every OOF turn; unrelated turns produce zero content answers; no regression in the 8 PaisaLo goldens + SOT suite.
- ONE live PREDUE call (on-script) + ONE deliberately messy call (Nitish improvises complaints/questions) — dump oof_class distribution, gate verdicts, confirm rate.
✋ FINAL CHECKPOINT W2 → sign-off → PaisaLo scenario ladder (ondue → postdue1 → postdue3 → NPA).

## Dependencies on Nitish/client (block W2-3, not W2-1/2)
- Fragment Library client sign-off (packet with 12-item copy list + 8 data-feed fields).
- Decisions D-P1…P8 (one line each).
- MFI scope answer (adds product-gated copy day if yes).

## Effort
W2-1: 1d · W2-2: 2d (executor split is the meat) · W2-3: 2.5d (router contract + unrelated lane + diversion ladder added) · W2-4: 1d + shadow week (calendar, overlaps W1 tail/UAT). Total ≈ 6.5 Cursor-days.
