# Phase 2 — Intelligence Engines (Complete)

**Status:** Phase 2 complete (Sprints 8–13)  
**Builds on:** [Phase 1 — Collections Dialogue Engine](./PHASE_1_COMPLETE.md) (Sprints 0–7)  
**Last updated:** June 2026

This document describes everything delivered in Phase 2: six additive intelligence engines that write **input signals** into the existing Phase 1 spine. Phase 2 does **not** replace the turn loop, compliance gate, or safety pre-empt. The gate remains the final authority on every outbound line.

---

## Table of contents

1. [Executive summary](#executive-summary)
2. [Design invariants (Phase 2)](#design-invariants-phase-2)
3. [Architecture — where Phase 2 sits](#architecture--where-phase-2-sits)
4. [Updated turn pipeline](#updated-turn-pipeline)
5. [Sprint delivery log](#sprint-delivery-log)
6. [Engine reference](#engine-reference)
7. [Signal flow and borrower record](#signal-flow-and-borrower-record)
8. [Decision overlay and recovery](#decision-overlay-and-recovery)
9. [Hot path vs persist path](#hot-path-vs-persist-path)
10. [Testing and merge gate](#testing-and-merge-gate)
11. [Git history](#git-history)
12. [Known limitations and Phase 3 scope](#known-limitations-and-phase-3-scope)
13. [Items pending product / compliance sign-off](#items-pending-product--compliance-sign-off)

---

## Executive summary

Phase 2 layers **deterministic scoring and classification** on top of the Phase 1 engine. Each engine reads from (and writes to) the durable borrower record and live call slots. None of them emit borrower-facing text or bypass `gate()`.

| Blueprint engine | Module | Sprint | Hot path | Persist path |
|------------------|--------|--------|----------|--------------|
| Trust Score (4) | `app/engines_p2/trust.py` | 8 | Hydrate cached score | Recompute on persist |
| Behavioral Risk (5) | `app/engines_p2/risk.py` | 9 | Hydrate cached flags | Recompute on persist |
| Persona (2) | `app/engines_p2/persona.py` | 10 | Hydrate cached persona | Rules recompute on persist |
| Emotion (3) | `app/engines_p2/emotion.py` | 11 | Rules classify per turn | Append to `emotions` |
| Decision overlay (8 full) | `app/engines_p2/decision_overlay.py` | 12 | Rank compliant actions | — |
| Recovery Probability (6) | `app/engines_p2/recovery_prob.py` | 13 | Hydrate cached score | Heuristic on persist |

At the end of Phase 2:

- **189 automated tests** pass in CI mode (`pytest tests -m "not live_kb and not live_vertex and not live_upstash"`).
- **≤ 1 LLM call per turn** unchanged — persona/emotion LLM rubric paths exist for offline use only.
- **`tests/compliance/`** remains the merge gate — all Phase 2 engines have explicit “input not license” tests.

---

## Design invariants (Phase 2)

1. **Input only, not license.** Every engine exports an assert constant (`*_IS_INPUT_NOT_LICENSE` or `OVERLAY_IS_INPUT_NOT_GATE`). Trust, persona, risk, emotion, recovery, and overlay scores **never** authorize conduct the gate prohibits.
2. **Gate absolute.** `app/engine/gate.py` runs last on every outbound line; Phase 2 cannot weaken it.
3. **No second blocking LLM call.** Hot-path classification uses deterministic rules. Optional LLM rubric helpers (`classify_persona_llm`, `classify_emotion_llm`) are for batch/offline paths only.
4. **Human-owned actions never auto-executed.** Settlement, restructuring, legal-prep, and vulnerable case review are recommended/routed only — the overlay and recovery engines do not invoke them.
5. **λ2 = ∞ in decision overlay.** Non-compliant action candidates are excluded from ranking; compliance is never traded off for recovery value.
6. **Fairness guardrail on risk.** Detectors operate on behavior history only — never identity or geography.

---

## Architecture — where Phase 2 sits

```
                    ┌─────────────────────────────────────────────────────────┐
                    │                    POST /turn                          │
                    └─────────────────────────┬───────────────────────────────┘
                                              │
     ┌────────────────────────────────────────▼────────────────────────────────────────┐
     │                         handle_turn (app/engine/turn.py)                         │
     └────────────────────────────────────────┬────────────────────────────────────────┘
                                              │
  LOAD ──► hydrate trust / risk / persona / recovery (cached)                             │
           classify emotion (rules, this turn)                                          │
           safety_preempt ◄── emotion hopelessness bridge                                 │
                                              │
  CORE (Phase 1) ──► retrieval ──► command_gen (1× LLM) ──► apply ──► priority          │
                                              │
  PHASE 2 ──► decision_overlay (rank compliant actions, set ptp_max_days, strategy)     │
              executor ──► nlg (tone_register from emotion) ──► gate ◄── FINAL          │
                                              │
  PERSIST ──► sync trust / risk / emotion / persona / recovery ──► audit (+ recovery)   │
```

Phase 2 engines **feed** the decision overlay, NLG tone selection, and safety bridge. They do **not** sit after the gate.

---

## Updated turn pipeline

Changes from Phase 1 are **bold**.

| Step | Module | Notes |
|------|--------|-------|
| 1–2 | `memory.load_state` / `load_borrower` | Unchanged |
| 3 | `tracker.hydrate_from_borrower` | + `trust`, `risk_flags`, `persona`, `emotion`, `recovery` |
| 4 | **`apply_trust_to_state`** | Cached trust → `slots.trust` |
| 5 | **`apply_risk_to_state`** | Cached risk flags → `slots.risk_flags` |
| 6 | **`apply_persona_to_state`** | Cached persona → `slots.persona` |
| 7 | **`apply_recovery_to_state`** | Cached `p_cure` / PV → `slots.recovery` |
| 8 | **`classify_emotion_from_turn`** | Rules on transcript; optional `turn_meta.prosody` hook |
| 9 | **`apply_emotion_to_state`** | `slots.emotion`, `emotion_intensity`, `tone_register` |
| 10 | **`safety_preempt`** | + emotion hopelessness-high bridge; early exit unchanged |
| 11–14 | retrieval → command_gen → apply → priority | Unchanged (still 1× LLM) |
| 15 | **`decision_overlay.apply_decision_overlay`** | Quadrant routing, `ptp_max_days`, action ranking |
| 16–18 | executor → nlg → gate | NLG uses `tone_register`; `validate_ptp` reads `ptp_max_days` |
| 19 | **persist** | + `sync_emotion`, `sync_persona`, `sync_recovery`; audit includes `recovery` |

---

## Sprint delivery log

| Sprint | Theme | Commit | Key deliverable |
|--------|-------|--------|-----------------|
| **8** | Trust Score Engine | `c877bdc` | Recency-weighted 0–100 trust; blueprint §10.3 paths |
| **9** | Behavioral Risk Engine | `c877bdc` | Pattern detectors; fairness; `slots.risk_flags` |
| **10** | Persona Engine | `976935f` | 15 personas; transition log; rules on persist |
| **11** | Emotion Engine | `aed4b7e` | 11 emotions + intensity; tone register; safety bridge |
| **12** | Decision overlay | `edbf327` | Objective function; Ability×Willingness routing |
| **13** | Recovery Probability | `a62785b` | Heuristic `p_cure` + expected PV; ML path documented |

---

## Engine reference

### Trust (`trust.py`) — Sprint 8

- **Formula:** `T_n = clamp(T_{n-1} + δ × recency_weight × magnitude_scale, 0, 100)` with kept-promise anchor.
- **Writes:** `BorrowerRecord.trust_current`, `trust_history`, `slots.trust`.
- **Tests:** `tests/unit/test_trust_engine.py`, `tests/fixtures/trust_blueprint_paths.py`.

### Risk (`risk.py`) — Sprint 9

- **Detectors:** excuse recycling, promise breaking, ghosting (borrower-relative), settlement fishing, strategic default, fraud indicator (corroboration required for serious labels).
- **Writes:** `BorrowerRecord.risk_flags`, `slots.risk_flags`.
- **Tests:** `tests/unit/test_risk_engine.py`.

### Persona (`persona.py`) — Sprint 10

- **15 personas:** genuine_payer, forgetful, salary_dependent, temporary_hardship, chronic_tomorrow, promise_breaker, strategic_defaulter, settlement_hunter, genuine_settlement_candidate, ghost, angry, dispute, fraud_claimant, wrong_number, vulnerable.
- **Classifier:** Deterministic rules on persist; optional `classify_persona_llm` off hot path.
- **Writes:** `BorrowerRecord.persona_current`, `persona_history`, `slots.persona`.
- **Tests:** `tests/unit/test_persona_engine.py`.

### Emotion (`emotion.py`) — Sprint 11

- **11 emotions:** anxiety, fear, stress, anger, frustration, hopelessness, shame, remorse, confusion, optimism, neutral + intensity (low/med/high).
- **Tone feed:** `select_tone_register(bucket, emotion, intensity)` → NLG `tone_register` variants.
- **Safety bridge:** high-intensity hopelessness → existing `safety_preempt` (no duplicated logic).
- **Prosody seam:** `turn_meta.prosody` → `ProsodyFeatures` (voice layer fills later).
- **Writes:** `slots.emotion`, `BorrowerRecord.emotions` history.
- **Tests:** `tests/unit/test_emotion_engine.py`.

### Decision overlay (`decision_overlay.py`) — Sprint 12

- **Objective:** maximize `E[recovery] − λ1·contact − λ3·experience` with λ2 = ∞ for non-compliant candidates.
- **Quadrants:** CAN& WILL, WANTS& CAN'T, CAN& WON'T, CAN'T& WON'T from cached persona ability/willingness.
- **Ranks** executor-allowable actions only; sets `ptp_max_days`, `pressure_allowed`, `decision_strategy`.
- **Tests:** `tests/unit/test_decision_overlay.py`.

### Recovery probability (`recovery_prob.py`) — Sprint 13

- **Heuristic v1:** base rate by bucket → adjusted by trust, persona, payment/PTP history, risk flags.
- **Writes:** `BorrowerRecord.recovery = {p_cure, expected_recovery_pv, last_scored, method: "heuristic_v1", explain}`.
- **ML path:** documented in module docstring — audit log is training-data source; swap to `method="ml_v1"` when data-gated.
- **Overlay input:** `recovery_effort_boost()` raises compliant effort priority from cached `p_cure` / PV.
- **Audit:** `TurnAuditChain.recovery` snapshot at persist.
- **Tests:** `tests/unit/test_recovery_prob.py`.

---

## Signal flow and borrower record

Phase 2 extends the 14-block `BorrowerRecord` (`app/schemas/state.py`):

| Block | Phase 2 fields |
|-------|----------------|
| Trust | `trust_current`, `trust_history` |
| Risk | `risk_flags` |
| Persona | `persona_current`, `persona_history` |
| Emotion | `emotions[]` (date, emotion, intensity, channel) |
| Recovery | `recovery` (p_cure, expected_recovery_pv, last_scored, method, explain) |

Live call slots hydrated at turn start (see `tracker.hydrate_from_borrower`):

```text
slots.trust, slots.risk_flags, slots.persona, slots.recovery,
slots.emotion, slots.emotion_intensity, slots.tone_register,
slots.decision_quadrant, slots.decision_strategy, slots.ptp_max_days, …
```

---

## Decision overlay and recovery

**Overlay reads (does not recompute):** trust, risk_flags, persona, emotion, bucket, recovery.

**Overlay writes:** ranked compliant actions, PTP policy window, pressure allowance, human recommendations (not executed).

**Recovery on persist** runs after trust/risk/persona sync so the heuristic sees fresh signals. Next turn hydrates `slots.recovery` for overlay boost.

**Trust → PTP window (overlay + actions):**

| Trust | `ptp_max_days` |
|-------|----------------|
| ≥ 75 | 21 |
| ≥ 45 | 14 |
| < 45 | 7 |

---

## Hot path vs persist path

| Engine | Hot path (per turn) | Persist path |
|--------|---------------------|--------------|
| Trust | Hydrate | `sync_trust_on_persist` — recompute |
| Risk | Hydrate | `sync_risk_on_persist` — recompute |
| Persona | Hydrate | `sync_persona_on_persist` — rules recompute |
| Emotion | Rules classify transcript | `sync_emotion_on_persist` — append history |
| Recovery | Hydrate | `sync_recovery_on_persist` — heuristic recompute |
| Decision overlay | `apply_decision_overlay` before executor | — |

**LLM budget:** still exactly one call (`command_gen`) on the normal turn path. Verified by latency tests in persona, emotion, recovery, and overlay test modules.

---

## Testing and merge gate

```powershell
# CI-equivalent (stub/sim clients)
pytest tests -m "not live_kb and not live_vertex and not live_upstash"

# Compliance merge gate (must stay green)
pytest tests/compliance/ -m compliance

# Phase 2 unit suites
pytest tests/unit/test_trust_engine.py tests/unit/test_risk_engine.py `
  tests/unit/test_persona_engine.py tests/unit/test_emotion_engine.py `
  tests/unit/test_decision_overlay.py tests/unit/test_recovery_prob.py
```

| Suite | Role |
|-------|------|
| `tests/compliance/` | Release gate — prohibited language, dispute hold, safety, audit chain |
| `tests/unit/test_*_engine.py` | Per-engine DoD, monotonicity, determinism, gate-not-relaxed |
| `tests/golden/` | Multi-turn executor and handle_turn E2E |
| `tests/unit/test_latency.py` | Stage timing; LLM call budget |

**Current count:** 189 passed (CI mode), 14 deselected live integration tests.

---

## Git history

| Commit | Message |
|--------|---------|
| `c877bdc` | feat: Phase 1 engine + Trust and Risk engines (Sprints 0-9) |
| `976935f` | feat: Persona Engine (Sprint 10) |
| `aed4b7e` | feat: Emotion Engine (Sprint 11) |
| `edbf327` | feat: Decision objective-function overlay (Sprint 12) |
| `a62785b` | feat: Recovery Probability Engine (Sprint 13) |

---

## Known limitations and Phase 3 scope

### Phase 2 limitations

1. **Heuristic recovery only** — `method="heuristic_v1"` until production outcome labels accumulate (see `recovery_prob.py` docstring).
2. **Persona/emotion LLM rubrics** — implemented but not on hot path; fine-tuned small models are a future DECISION NEEDED.
3. **Prosody** — interface only; acoustic features come from the voice layer via `turn_meta.prosody`.
4. **Overlay action catalog** — synthetic candidates extend flow steps; new flows need category mapping in `decision_overlay.py`.
5. **Dashboard** — recovery/persona/trust signals are in the borrower record and audit; a separate dashboard UI is out of scope for this repo.

### Phase 3 (roadmap — not started)

Per BUILD_SPEC appendix:

- Omnichannel identity resolution across voice/WhatsApp
- Predictive collections (pre-emptive intervention using recovery model)
- Autonomous recovery within strict policy (humans retain settlement/legal/vulnerable decisions)

---

## Items pending product / compliance sign-off

Inherited from Phase 1 (unchanged) plus Phase 2 notes:

| Item | Location / note |
|------|-----------------|
| Recovery label definition (`cured_within_N_days`) | `recovery_prob.py` — DECISION NEEDED before ML training |
| Emotion distress vs tone-only “can't pay” | `compliance_defaults.DISTRESS_SIGNALS` trimmed in Sprint 11; confirm with Compliance |
| v1 languages and tone templates | `nlg.py`, flow YAML `tone_register` variants |
| All Phase 1 `# DECISION NEEDED` items | See [Phase 1 doc § Items pending compliance sign-off](./PHASE_1_COMPLETE.md#items-pending-compliance-sign-off) |

---

## Document history

| Date | Change |
|------|--------|
| June 2026 | Phase 2 complete — Sprints 8–13 delivered and documented |

For the original build specification, see `Collection_agent_spec` at the repository root.  
For go-live readiness, see [GO_LIVE_CHECKLIST.md](./GO_LIVE_CHECKLIST.md).
