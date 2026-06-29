# Go-Live Checklist — Collections Dialogue Engine

**Scope:** Phase 1 + Phase 2 complete (Sprints 0–13)  
**Audience:** Engineering, Compliance/Legal, Operations  
**Last updated:** June 2026

Use this checklist before pointing production traffic at the engine. Every **Gate** item must be green; **Verify** items should be explicitly signed off.

Related docs: [PHASE_1_COMPLETE.md](./PHASE_1_COMPLETE.md) · [PHASE_2_COMPLETE.md](./PHASE_2_COMPLETE.md)

---

## 1. Merge gate (automated — must pass)

Run locally or in CI before any production deploy:

```powershell
cd c:\Users\nitis\source\repos\Collection
.\.venv\Scripts\Activate.ps1
pytest tests -m "not live_kb and not live_vertex and not live_upstash"
pytest tests/compliance/ -m compliance
ruff check app tests
```

| # | Check | Expected |
|---|--------|----------|
| G1 | Full CI test suite | 189+ passed, 0 failed |
| G2 | Compliance suite | All `@pytest.mark.compliance` green |
| G3 | Ruff lint | All checks passed |
| G4 | No secrets in git history | `.env`, `secrets/`, `*-sa.json` untracked |

---

## 2. Compliance and legal sign-off

| # | Item | Owner | Status |
|---|------|-------|--------|
| C1 | Prohibited outbound phrase list finalized | Compliance | ☐ |
| C2 | Call-window hours (default 08:00–19:00 IST) approved | Compliance | ☐ |
| C3 | Daily attempt cap (default 3) approved | Compliance | ☐ |
| C4 | Vulnerability / distress signal lists reviewed | Compliance | ☐ |
| C5 | Care-first reply and safe-fallback text approved | Compliance | ☐ |
| C6 | Emotion hopelessness → human handoff path reviewed | Compliance | ☐ |
| C7 | Phase 2 “input not license” posture acknowledged (trust/persona/risk/emotion/recovery never bypass gate) | Compliance | ☐ |
| C8 | Audit retention policy (Redis hot + durable export) agreed | Legal / Eng | ☐ |

---

## 3. External integrations

| # | Service | Env vars | Verify |
|---|---------|----------|--------|
| E1 | **Upstash Redis** (state + borrower + audit) | `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN` | ☐ `MEMORY_MODE=live` — run `tests/unit/test_live_upstash.py` |
| E2 | **Vertex Gemini** (command gen) | `GCP_PROJECT`, `GCP_REGION`, `GEMINI_MODEL`, credentials | ☐ `LLM_MODE=live` — run `tests/unit/test_live_vertex.py` |
| E3 | **Fonada KB** (flow retrieval) | `KB_URL`, `KB_API_KEY` | ☐ `KB_MODE=live` — run `tests/unit/test_live_kb.py` |
| E4 | **Tools / LMS API** (read + write actions) | `TOOLS_URL`, `TOOLS_MODE=live` | ☐ Sandbox idempotency + dispute/PTP paths |
| E5 | **Voice layer** (external) | ASR/TTS/telephony via Fonada | ☐ Text-in → engine → text-out contract only |

---

## 4. Configuration and tenancy

| # | Item | Notes | Status |
|---|------|-------|--------|
| T1 | Production `.env` populated (no defaults in prod) | Copy from `.env.example`; never commit | ☐ |
| T2 | `TENANT_ID` / per-lender config plan | v1: env-backed; multi-tenant file TBD | ☐ |
| T3 | PII masking in logs verified | `tests/unit/test_pii.py` | ☐ |
| T4 | Service account keys stored outside repo | `vertex-sa.json` in `.gitignore` | ☐ |
| T5 | Health endpoint monitored | `GET /healthz` returns client connectivity | ☐ |

---

## 5. Engine behaviour verification (staging)

Run scripted scenarios against staging with live clients:

| # | Scenario | Pass criteria |
|---|----------|---------------|
| B1 | PTP within policy | Confirm + schedule; `ptp_allowed=true`; audit chain complete |
| B2 | PTP too far | Counter-offer; no schedule; trust-adjusted window respected |
| B3 | Dispute + parked PTP | Dispute first; promise resumed after drop |
| B4 | Vulnerability / crisis | Safety pre-empt; transfer; no collection pressure in reply |
| B5 | Hopelessness (emotion) | Care-first; human handoff; recovery suspended |
| B6 | Adversarial threat elicitation | Gate blocks/modifies; no prohibited phrases outbound |
| B7 | Opt-out / out-of-window | Silent or block per config |
| B8 | Low trust borrower | Shorter PTP leash (7 days); overlay `pressure_allowed` where applicable |
| B9 | Multi-turn same borrower | State + borrower record persist; trust/risk/persona/recovery update |

Golden references: `tests/golden/test_handle_turn_e2e.py`, `tests/golden/test_adversarial_e2e.py`.

---

## 6. Latency and capacity

| # | Item | Target | Status |
|---|------|--------|--------|
| L1 | Engine p95 (excl. network) | ≤ 700 ms documented | ☐ Run `tests/unit/test_latency.py`; review stage breakdown |
| L2 | LLM calls per turn | ≤ 1 on normal path | ☐ Confirmed in audit `llm_calls` field |
| L3 | KB retrieval timeout | Fail-soft → clarify | ☐ |
| L4 | Tool failure degradation | Handoff, no crash | ☐ `tests/unit/test_tools_sprint3.py` |
| L5 | Concurrent calls / Redis TTL | `STATE_TTL_SECONDS` (default 4h) appropriate | ☐ |

---

## 7. Observability and audit

| # | Item | Status |
|---|------|--------|
| O1 | OpenTelemetry turn spans enabled (if required) | ☐ |
| O2 | Every turn produces `audit_id` + reconstructable chain | ☐ |
| O3 | Audit includes: flows, commands, actions, gate verdict, **recovery snapshot** | ☐ |
| O4 | Alerting on gate CRITICAL / safety pre-empt rate (ops-defined) | ☐ |
| O5 | Durable audit export cadence (Postgres / S3) if required for RBI retention | ☐ |

Query example: `query_turn_audits_by_borrower(memory, borrower_id)` in `app/memory/audit.py`.

---

## 8. Phase 2 intelligence (post-go-live monitoring)

| # | Signal | Monitor for |
|---|--------|-------------|
| P1 | Trust distribution | Drift vs expected portfolio |
| P2 | Risk flag rates | False positive rate on serious labels |
| P3 | Persona transitions | Unexpected churn genuine_payer → strategic_defaulter |
| P4 | Emotion → tone | De-escalation templates firing on anger/fear |
| P5 | Recovery `p_cure` | Heuristic sanity vs actual cure rates (feeds ML swap decision) |
| P6 | Overlay quadrant mix | WANTS& CAN'T cases not receiving pressure actions |

**ML swap gate (future):** Do not enable `method="ml_v1"` until hold-out calibration beats `heuristic_v1` and passes fairness review (`recovery_prob.py`).

---

## 9. Deployment checklist

| # | Step | Status |
|---|------|--------|
| D1 | Build container / deploy artifact from `master` at known commit | ☐ |
| D2 | Run full test suite on deploy branch | ☐ |
| D3 | Smoke `POST /turn` on staging | ☐ |
| D4 | Canary traffic (% or internal borrowers only) | ☐ |
| D5 | Rollback procedure documented (previous image + Redis compatibility) | ☐ |
| D6 | On-call runbook: safety spike, gate block rate, KB/LLM outage | ☐ |

---

## 10. Sign-off

| Role | Name | Date | Signature |
|------|------|------|-----------|
| Engineering lead | | | ☐ |
| Compliance / Legal | | | ☐ |
| Product owner | | | ☐ |
| Operations / Fonada integration | | | ☐ |

---

## Quick reference — production env modes

```env
MEMORY_MODE=live
LLM_MODE=live
KB_MODE=live
TOOLS_MODE=live   # or simulate for pilot — confirm with ops
```

**Never** set `LLM_MODE=live` without Vertex quota and model ID confirmed.  
**Never** commit `.env`, service account JSON, or API keys.

---

## Document history

| Date | Change |
|------|--------|
| June 2026 | Initial go-live checklist — Phase 1 + Phase 2 complete |
