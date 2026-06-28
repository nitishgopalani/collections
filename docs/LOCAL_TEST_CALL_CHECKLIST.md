# Local test call checklist (server test stack)

Use this after standing up local Postgres + brain wiring. **Revert call-window overrides after testing.**

## 1. Stack health

```bash
curl -sf http://127.0.0.1:8000/healthz | jq .
```

Confirm:

- `status`: `ok`
- `borrower_db_enabled`: `true`
- `clients.borrower_db`: `true`
- `client_modes.borrower_db`: `postgres`
- Brain startup log contains: `borrower postgres connected (local test DB)`

## 2. Local Postgres (no Supabase)

```bash
docker compose -f deploy/docker-compose.yml ps postgres
python scripts/apply_borrower_migrations.py   # from Collection repo in brain container or dev env
```

Confirm seeded borrower (replace phone in seed SQL with your E.164 test number):

- `name=Rajesh`, `amount_due=350`, `tenant_id=default`

## 3. Borrower match on call

Place a test call from the seeded phone. In brain logs at `session_start`:

- `borrower matched by phone` or `session borrower resolved borrower_id=B_RAJESH name=Rajesh amount_due=350`

On first turn, state slots should include `borrower_name=Rajesh`, `amount_due=350` (hydrated — not set by Gemini).

## 4. Call window + modes (temporary for pilot)

In server `.env` (not committed):

- `CALL_WINDOW_START=00:00`, `CALL_WINDOW_END=23:59`, `CALL_WINDOW_TIMEZONE=Asia/Kolkata` — **revert after test**
- `AMD_ENABLED=false`
- `TOOLS_MODE=simulate`
- `STUB_MODE=false`, `LLM_STUB=false`, `KB_STUB=false`
- **Do not** set `force_flow` — default dynamic routing (identity gate, then KB candidates)

Recreate brain + go-server after env changes.

## 5. Log capture greps

```bash
# ASR final + reply chunks + timing
grep -E 'asr final|reply chunk|turn timing|flow_class|gate_verdict|send_payment_link|command_gen|identity_response|start_flow' /path/to/go-server.log

# Brain: flow routing + slot validation
grep -E 'session borrower resolved|command_gen|start_flow|identity_response|rejected unknown slot' /path/to/brain.log
```

Success signals:

- No `command_gen: rejected unknown slot borrower_name`
- `identity_response` set on identity turn (not looping identity prompt)
- Different utterances → different `start_flow` targets (`promise_to_pay`, `dispute`, `hardship`, …)
- `gate_verdict=allow` on in-window test calls

## 6. Simulator (pre-call sanity)

```bash
python scripts/verify_dynamic_routing.py
python scripts/seed_kb_flows.py   # requires KB_API_KEY; prints seeded flow count
```
