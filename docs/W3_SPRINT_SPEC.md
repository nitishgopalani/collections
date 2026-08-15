# SPRINT W3 — Commitments & Context
_v1.0 · 15 Aug 2026 · Base: brain 2e162f6 (ladder closed), go-server ce77494 · ~6 Cursor-days · Checkpoint discipline unchanged (WORKLOG entry + tracker + sha per phase, STOP each ✋)._
_Ladder already proved the PTP mechanics (relative-date coercion, ≤30d bound, date readback, restatement-confirm). W3 adds POLICY + MEMORY + OBLIGATIONS around them._
_Canonical copy of `scripts/sprint W3.md`._

## Client-input defaults (proceed on these; PENDING-CLIENT labels, revisit at demo)
- C-3 PTP rules: max_ptp_days=30 (ladder-proven) · vague→ask concrete date (live) · >30d → counter with nearest acceptable date, one time, then accept-with-flag `ptp_beyond_policy` · min_partial_pct=25% of repay_amount · partial → ack + ask remainder date.
- C-2 multi-loan: highest-DPD loan wins hydration; `multi_loan=true` logged when >1 active row.
- C-4 inbound DID: scripted greeting + branch/helpline referral + callback-capture; no full flows inbound yet.

## W3-1 — PTP Policy Engine + computed slots (1.5d) ✋ CP-W31
- `ptp_policy.py` pure function: (committed_date, offered_amount, policy_yaml, today) → {accept | counter(date) | accept_flagged | ask_remainder}. Tenant YAML: max_ptp_days, min_partial_pct, counter_max_attempts=1.
- Wire at the date-confirm seam (post-gate execute): accept → `ptp_date`+`ptp_amount` slots (source=confirmed) + disposition `PTP_SET` + assurance-with-date close (exists). Counter → approved counter fragment ("इतना आगे नहीं — क्या {counter_date} तक हो पाएगा?").
- Partial: "aadha aaj dunga" class → offered_amount extract (digits + Hindi number words) → ≥min_pct: ack + remainder-date ask; <min_pct: polite full-ask fragment.
- Computed slots (deterministic Python, renderer-visible): remaining_after(offered), days_to_due, days_since_due — fragments may reference them; LLM never computes.
- Tests: date beyond 30d → counter once then flag; partial 50% → remainder ask; partial 10% → full-ask; ladder L3/L4 replays unchanged (regression).

## W3-2 — Call-history + mid-call memory (1.5d) ✋ CP-W32
- Hydration adds from sessions store: `attempts_today`, `last_disposition`, `last_ptp_date`, `last_call_ts`. Repeat-call greeting variant when last_call_ts < 24h: "आज पहले भी बात हुई थी…" (fragment, PENDING-CLIENT wording) — never re-dump the full detail greeting.
- Existing PTP honoured: if last_ptp_date in future and today's campaign dials anyway → PTP-reminder script variant, not fresh collect (bot must not contradict its own record).
- Mid-call payment claim ("abhi kiya QR se") → `fact_payment_lag` fragment (exists) + `payment_claimed=true` disposition flag; mid-call re-hydrate trigger (one refetch, throttled) if tools_live present, else flag only.
- Tests: repeat-call greeting fires on 2nd seeded call; PTP-reminder path; claim flag set.

## W3-3 — Post-call obligation loop (1.5d) ✋ CP-W33
- Dispositions export: per-call record (session, borrower, disposition, ptp fields, flags) → `exports/` CSV per day + optional webhook stub (client API later).
- Callback honour: `repair_callback_scheduled` + `callback_request` dispositions → re-queue file the dialer layer can consume (W4 wires the dialer; W3 produces the truth).
- Flagged worklist: daily file of VULNERABLE_FLAGGED / THIRD_PARTY_FLAGGED / complaint_raised / dnc_requested for human review.
- Invariant: bot's spoken promises = rows in these files, same day. Test: ladder sessions replayed → correct rows.

## W3-4 — Edges + debt (1.5d) ✋ CP-W34
- Inbound DID: answer path → scripted greeting (identity-safe) + helpline/branch + callback capture → disposition `INBOUND_RETURN`.
- LLM-429/timeout degrade: on command_gen failure → deterministic-only turn (Tier-1 + compose-less re-ask), `llm_degraded=true`; call survives.
- Multi-loan rule (C-2 default) in hydration + test.
- Persist async: Upstash writes off the reply critical path (queue + flush; opener F1 root cause closed properly).
- DEBT-038 prewarm: slot-segmented cache keys (static prefix/suffix split around {customer_name} etc.) + boot warm. DEBT-043 consent enum forms. DEBT-044 m2e=0 go-server timing fix.

## Exit bar (W3 CLOSED when)
Replay: ladder + PTP fixtures green. One live call: postdue3 with "20 din baad" beyond-policy date → counter heard → accept → PTP_SET row lands in export file same day. Repeat-call greeting heard on a second dial. Tracker ~82%.
