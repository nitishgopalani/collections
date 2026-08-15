# Tools API contract

_W4-3 / DEBT-029. Two endpoints. Integration surface stays small so a tenant
(PaisaLo or anyone else) can say yes without a 20-field LMS spec._

Brain modes (`TOOLS_MODE`):

| Mode | When | Behaviour |
|---|---|---|
| `stub` | **UAT default** | Reads borrower-state from the local Postgres seed (same rows the brain already hydrates). No hangups, no payment-link sims, no injected actions. |
| `live` | Pilot / MPLS | HTTP to `TOOLS_URL`. Timeout **2s**, **one retry**, then degrade: keep the hydrated snapshot and (if the turn was a payment-state claim) speak `fact_payment_lag`. Call survives. |
| `simulate` | Lab / goldens only | In-memory fixtures. **Startup fails loudly when `CARRIER=asterisk`** — same class of refuse as `ASR_ENABLED`/`TTS_ENABLED` off on the go-server. |

`tools_client` and `tools_mode` are logged on `session_start`. Each turn's
guards carry `tool_call_ms` and `tool_degraded`.

---

## 1. `GET /v1/borrower_state`

Lookup by **loan_ref** or **phone** (either is enough).

```
GET {TOOLS_URL}/v1/borrower_state?loan_ref=LN-RAJ-001
GET {TOOLS_URL}/v1/borrower_state?phone=9810587857
```

Optional: `borrower_id`, `tenant_id`.

**200 body**

```json
{
  "outstanding": 4500,
  "last_payment": { "date": "2026-07-01", "amount": 4500 },
  "ptp_on_file": { "date": "2026-08-20", "amount": 4500 }
}
```

- `last_payment` / `ptp_on_file` are `null` when none exist.
- Unknown borrower: `200` with `"found": false` (do not 404 the call).
- Brain maps `outstanding` → `amount_due`, `last_payment.date` → `last_date_paid`,
  `ptp_on_file.date` → `committed_date`.

Timeout / 5xx / network: brain retries once, then `tool_degraded=true` and
answers from the snapshot already on the session.

---

## 2. `POST /v1/disposition` (optional)

Mirrors the W3-3 export row. Fire-and-forget; a 5xx does not fail the call.

```
POST {TOOLS_URL}/v1/disposition
```

```json
{
  "session_id": "…",
  "borrower_id": "…",
  "tenant": "paisalo",
  "scenario": "postdue3",
  "disposition": "PTP_SET",
  "ptp_date": "2026-08-20",
  "ptp_amount": 4500,
  "flags": ["payment_claimed"],
  "call_ts": "2026-08-15T12:00:00+05:30",
  "duration": 94
}
```

Same fields as `exports/dispositions_YYYYMMDD.jsonl` (`app/engine/obligation_export.py`
`RECORD_FIELDS`). Extra keys are ignored.

Stub mode records this locally (no HTTP). Live mode POSTs it; degrade on failure.

---

## What we will not ask for

No write-LMS, no hangup, no payment-link, no schedule-callback RPC. Those stay
on our side (exports + dialer + orchestrator). Two reads/writes is the whole
ask — if the tenant can expose `get_borrower_state`, we can go live.
