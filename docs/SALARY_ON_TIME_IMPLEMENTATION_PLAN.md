# Salary On Time — Implementation Plan & Design

Branch: `salary_on_time` (created in both `collections` and `websocket` repos)
Source script: `websocket/salary_on_time_flow` (Pre-Closure / On-Due / Post-Due + ~30 objections)
Status: PLAN — no engine code changed yet. This document is the build spec.

---

## 0. Decisions adopted (CONFIRMED by product)

| # | Decision | Confirmed choice |
|---|----------|------------------|
| D1 | RBI / compliance gate | **Per-tenant gate FULLY OFF** for `salary_on_time` — no collection-pressure rules **and no self-harm/safety pre-empt**. Bot follows the script verbatim with zero guardrails. ⚠️ See risk note in §5. Existing `default` (collections) tenant stays fully compliant (gate ON). |
| D2 | How the bot follows the script | **Strict scripted flows** — deterministic step-by-step exactly as written; the LLM only extracts slots (yes/no, dates, objection type). |
| D3 | `transfer_call` (~15 uses) | **Real transfer via existing endpoint** — product has a call-transfer endpoint. We call it on `transfer_call`. (Need the endpoint contract — see §8 / §13.) |
| D4 | WhatsApp + payment link | **Simulated** for this build/test phase: log what would be sent, use fake data. Live APIs later. |
| D5 | Money fields (offer/discount/penalty) | **Precomputed upstream** — your API/panel sends final `repay_amount`, `offer_amount`, `discount_amount`, etc.; engine only renders them. |
| D6 | Pre/On/Post-Due selection | **Auto-select** by comparing `due_date` to today. |
| D7 | Data source | **Prod (later):** panel writes to **Supabase Postgres**, engine reads it. **Now (testing):** local Postgres `borrowers` seeded via fake-data SQL. |
| D8 | Languages | **Hindi + English + Hinglish** variants authored for every response. |

---

## 1. Goal

Reproduce the Salary On Time collection script faithfully as a runnable agent on the existing voice stack (Asterisk → go-server → brain), driven by per-call campaign data, with the RBI compliance restrictions made optional so the script's persuasion (penalty / CIBIL / reload upsell) plays as written. Testable end-to-end with fake data seeded into the server Postgres.

Non-goals (this phase): real SIP transfer, live WhatsApp/payment APIs, real credit decisioning for re-loan.

---

## 2. How it fits the existing engine

We do **not** fork the engine. Salary On Time becomes:

1. A **new tenant** `salary_on_time` (its own `TenantConfig`, with the collection gate disabled).
2. A **new flow pack** = a set of YAML flows + responses authored from the script.
3. Selected per call via **`agent_id`** (e.g. `salary-on-time`) → `routing.py` maps it to the tenant + entry flow. The dialer sets `agent_id` in Asterisk metadata (already supported).
4. **Strict scripted** execution using the existing `steps/collect/utter/action/decide/next` flow machinery; the existing flow-stack parking/resume gives us "return to the step where the objection happened."

This keeps the compliant collections engine intact on the `default` tenant while Salary On Time runs alongside.

### Current engine pieces we reuse as-is
- Flow schema (`app/schemas/flow.py`): `collect / utter / action / decide / next`, branch `if/then/else`.
- Condition eval (`app/engine/conditions.py`): `== != < <= > >= in`.
- Identity name-confirmation (already built this session).
- Third-party detection scaffolding (`compliance_handoff.yml` → `third_party`).
- Tools: `send_payment_link` (WhatsApp link, simulated), `create_payment_link`, `schedule_followup`, `log_disposition`.
- `close_call` → `end_call` → go-server hangs up after playback (`tts_reply_consumer.go`).
- Per-call variables via `borrower_context` (needs extension — see §4).
- Deterministic QA via `agent_id` force-flow + once-per-call guard (added this session).

---

## 3. Architecture changes (high level)

```
Dialer/Panel ──campaign row──▶ Postgres (borrowers + salary_on_time fields)
        │ agent_id=salary-on-time, customer_phone
        ▼
   Asterisk ──session_start(metadata)──▶ go-server
        │  borrower_context: phone (+ optional inline fields)
        ▼
   go-server ──session_start──▶ brain (collections engine)
        │  resolve borrower by phone → load Salary On Time fields from DB
        ▼
   brain: tenant=salary_on_time, gate=OFF(+safety ON),
          section=auto(due_date), scripted flow pack
        │  chunks (TTS) / done(end_call|transfer disposition)
        ▼
   go-server → Sarvam TTS → caller ; end_call → hangup
```

---

## 4. Data model & supporting fields (the biggest enabling change)

### 4.1 Fields the script needs
`customer_name, repay_amount, due_date, discount_amount, offer_amount, loan_amount, disbursal_date, loan_tenure` (+ runtime-captured `customer_time`, `customer_committed_date`).

Currently `borrower_context` supports only: `borrower_name, phone, amount_due, account_ref, language`.

### 4.2 Where the data lives (D7)
- **Production (later):** the panel/API writes campaign rows into **Supabase Postgres**; the engine reads them. Since Supabase is Postgres, this is a connection-string + schema-alignment change to the borrower store (the `user-supabase-postgres` MCP is available for inspection/seeding).
- **Now (testing):** local Postgres `borrowers` table seeded with fake-data SQL. Same code path — only the DB URL differs.
- Per-call delivery (both cases): borrower resolved by phone at `session_start` (already working), then fields hydrate into conversation slots.

### 4.3 Schema migration (new columns)
New migration `migrations/002_salary_on_time_fields.sql` adding to `borrowers` (nullable, so existing rows/tenants unaffected):

| Column | Type | Notes |
|--------|------|-------|
| `repay_amount` | numeric | total due (principal+interest[+penalty]) |
| `offer_amount` | numeric | discounted "pay today" amount |
| `discount_amount` | numeric | savings if paid today |
| `due_date` | date | drives section selection |
| `loan_amount` | numeric | original principal |
| `disbursal_date` | date | for "never took a loan" objection |
| `loan_tenure_days` | int | tenure |
| `section_override` | text null | optional: force pre/on/post |

(Money precomputed upstream per D5.)

### 4.4 Contract & code extension
- **Go (`websocket/internal/brain/contract.go`)**: extend `BorrowerContextPayload` with the new fields (so the dialer *can* also send inline if not in DB). Optional for phase 1 (DB is primary).
- **Python (`app/ws/borrower_context.py`)**: extend `normalize_borrower_context` + `BORROWER_CONTEXT_SLOT_KEYS` + `apply_borrower_context_to_state/record` to carry the new fields into slots.
- **Python (`app/memory/postgres_borrowers.py`)**: `row_to_borrower` + `lookup_by_phone`/`load_borrower` SELECTs include the new columns; map into `BorrowerRecord.loan`.
- **Hydration (`app/engine/tracker.py` `hydrate_from_borrower`)**: surface the new loan fields as slots (`repay_amount`, `offer_amount`, `discount_amount`, `due_date`, `loan_amount`, `disbursal_date`, `loan_tenure`).

### 4.5 Fake data for testing
`scripts/seed_salary_on_time.sql` (run on server DB) inserting a few borrowers spanning the 3 sections:
- Pre-closure (due_date in future), On-due (due_date = today), Post-due (due_date past) — each with realistic amounts, plus a third-party test (phone belongs to "family"), and an "already paid" flagged row.

---

## 5. Compliance gate — per-tenant FULL OFF (D1)

- Add to `TenantConfig`: `enforce_compliance_gate: bool = True` (default = current strict behavior).
- `salary_on_time` tenant sets it **False** → the entire gate is bypassed for this tenant:
  - collection-pressure / prohibited-phrase / debt-disclosure / dunning checks: **off**
  - third-party privacy debt-block: **off** (Branch C1 family hears details, per script)
  - **self-harm / vulnerability safety pre-empt: also off** (per D1)
- Implementation: in `app/engine/gate.py` short-circuit to `verdict="allow"` when the tenant flag is off; in `app/engine/turn.py` skip `safety_check_transcript`/`safety_preempt` for that tenant. The `default` tenant path is untouched.

> ⚠️ **Risk note (acknowledged):** with the gate fully off there is no automated stop for self-harm/distress or for threatening/prohibited language. This is an explicit product decision for the Salary On Time pilot. Recommend revisiting before any broad rollout. Flag kept per-tenant so it can be re-enabled instantly.

This flag is the load-bearing change that makes the script playable verbatim while keeping the compliant collections engine intact on `default`.

---

## 6. Flow design (authored YAML)

New flow pack files under `app/flows/salary_on_time/` (or prefixed), loaded for the tenant:

### 6.1 Entry + identity (Step 1)
- `sot_identity`: greeting "main Salary On Time ki taraf se Rajveer… {customer_name} se baat ho rahi hai?" → name confirm (reuse name-confirmation verify).
- Branch on: confirmed self / knows-customer / doesn't-know (→ "update number" + `hangup_call`) / states relation immediately.

### 6.2 Branch C — third-party (1.4)
- `sot_third_party`: classify relation → C1 immediate family (proceed, third-person wording) / C2 sibling (cousin vs real → real proceeds, cousin → C3) / C3 restricted (no details, ask availability / alternate number).
- Uses the existing third-party scaffolding, but with the **privacy block relaxed for this tenant** so C1 family can hear loan details (per the script). C3 still withholds details (script-driven, not gate-driven).

### 6.3 Purpose + offer (Step 2) — section-aware
- `sot_offer_preclosure` / `sot_offer_ondue` / `sot_offer_postdue`: the three Step-2 scripts, chosen by section (see §7).
- Branch 2.2A yes → commitment; 2.2B no/reason → objection+push; 2.2C already-paid → screenshot/ref path.

### 6.4 Reason + push (Step 3)
- `sot_push`: ask reason → first push (savings/penalty angle) → second push (reload angle) → third push (post-due penalty angle) → ask for date. Each push is a scripted utter with a yes/no branch.

### 6.5 Payment commitment (Step 4) — date logic
- `sot_commit`: capture committed date + time. Branch: today / tomorrow / before-due / on-due / after-due — each with the scripted push/confirm lines. After-due-and-after-tomorrow → handoff (transfer degrade).
- Date normalization: LLM maps "aaj/kal/parso/shaam/dopahar" → normalized date + `customer_time`; engine compares to `due_date` via conditions.

### 6.6 Closing (Step 5)
- `sot_close`: send link (`send_whatsapp_message` tool, see §8) → speak closing → `hangup_call`. (Note: the script's literal "hang up THEN speak" is physically impossible; we do send-link → speak closing → hang up, which is the only sane order.)

### 6.7 Objection handling (~30)
- One `responses:` entry per objection + a routing layer. Two options:
  - (a) **KB-driven**: add KB docs tagged per objection; LLM classifies the objection → engine utters the matching response, then **resumes the prior step** (flow-stack parking already supports this).
  - (b) **decide-driven**: an `objection_type` slot the LLM sets, with a big `decide` mapping to responses.
- Recommended: (a) for naturalness + the "return to exact step" requirement. Each objection response is authored verbatim from the script; many end in the transfer-degrade action.

### 6.8 Tone & languages (D8)
- Every response authored in **Hindi + English + Hinglish** (`language: hi | en | hinglish`), using the script's Hindi/English lines and a Hinglish blend. The NLG language ladder picks per the borrower's `language` slot / session locale.

---

## 7. Section selection (D6)

- New local action `select_section` (or computed at hydrate): `today vs due_date` →
  - `due_date > today` → `pre_closure`
  - `due_date == today` → `on_due`
  - `due_date < today` → `post_due`
- Stored in slot `sot_section`; the entry flow `decide`s into the right Step-2 offer flow. `section_override` column wins if set (for QA).

---

## 8. Tooling

| Script tool | Engine mapping | Phase-1 behavior |
|-------------|----------------|------------------|
| `hangup_call` | `close_call` → `end_call` | Works (go-server hangs up after playback) |
| `send_whatsapp_message` | new action `send_whatsapp_message` (wraps `send_payment_link` + generic message) | Simulated: logs payload; returns fake link |
| `transfer_call` | new tool `transfer_call` → **calls your existing transfer endpoint** | Real transfer: utter handoff line → POST to your endpoint with the call identifier → endpoint moves the leg to a senior agent |

New action(s) registered in `app/engine/actions.py` (LOCAL_ACTIONS / WRITE_TOOLS) + sim/live entries in `app/clients/tools_sim.py`.

**Transfer endpoint integration (D3) — need spec from you:**
- Endpoint URL + method + auth (header/key).
- Request body: which identifier does it need — `stream_sid`, `customer_phone`, an Asterisk channel id, or a call/campaign id? (The brain has `session_id`=stream_sid and the phone; go-server holds the live channel.)
- Whether the brain should call it directly (it has `httpx`) **or** the brain should emit a `transfer` disposition and **go-server** calls the endpoint (preferred if the endpoint needs the live Asterisk channel). Decide based on what identifier the endpoint expects.
- Until the spec arrives, `transfer_call` is stubbed to "utter handoff line + log intent" so flows are testable; swapping in the real HTTP call is a one-function change.

---

## 9. Phased implementation plan

| Phase | Scope | Output | Depends on |
|-------|-------|--------|------------|
| **P1. Data plumbing** | Migration `002`, extend borrower store/contract/borrower_context/hydration, seed script with fake data | New fields flow phone→DB→slots; verified by unit tests | — |
| **P2. Tenant + gate switch** | `salary_on_time` TenantConfig, per-tenant gate flags, gate wrapped behind flags (safety ON) | Pressure lines pass for the tenant; default tenant unchanged | — |
| **P3. Section + identity + Branch C** | `select_section`, `sot_identity`, `sot_third_party` (privacy relaxed for tenant) | Call opens correctly, third-party branches work | P1,P2 |
| **P4. Core flow (Steps 2–5)** | offer (3 sections), push, commit (date logic), close | Happy-path + main branches end-to-end in sim | P3 |
| **P5. Tools** | `send_whatsapp_message`, `request_transfer` (degrade), wire closing order | Closing + transfer-degrade behave | P4 |
| **P6. Objections** | ~30 objection responses + classification/routing + resume | Objections handled, return to prior step | P4 |
| **P7. Test + QA** | Sim scenarios per section + objection goldens; `agent_id=salary-on-time` live QA; deploy + test call | Green suite + live verification | all |

Rough sizing: P1–P3 small/medium each; P4 + P6 are the bulk (content authoring + branch logic); P5 small (degrade); P7 medium.

---

## 10. Testing plan

- **Offline (no calls):** new `tests/sim/sot_*.json` scenarios (pre/on/post-due happy paths, third-party C1/C3, already-paid, date-after-due → transfer, 5–6 key objections) run through `tests/unit/test_flow_sim.py`; golden assertions for the mandatory closing order and transfer disposition. Run via the WSL venv pytest.
- **Live QA:** `agent_id=salary-on-time` (+ `section_override` for forcing a section) on a real call against seeded fake borrowers.
- **Regression:** existing `default`-tenant suites must stay green (proves the gate flag didn't break compliance behavior).

---

## 11. Server / deployment notes

- Run `migrations/002_salary_on_time_fields.sql` + `scripts/seed_salary_on_time.sql` on the server Postgres (the migration is additive/nullable — safe for existing data).
- Brain image rebuilt from `salary_on_time` branch via your pipeline; go-server image too if P1 contract extension is included (note: go-server image is prebuilt/not buildable on the server — must come from your build pipeline).
- Verify with `docker compose logs brain` (`session_start received`, `borrower resolved`, `turn_decision`).

---

## 12. Not possible / out of scope this phase (explicit)

1. **Live WhatsApp / payment-link generation** — simulated (D4). Live = wire provider APIs + keys, set `TOOLS_MODE=live`.
2. **Literal "hang up then speak"** — impossible; we do send→speak→hangup.
3. **Real re-loan eligibility/limit computation** — the "20% enhanced limit / 2-minute reloan" lines are spoken as scripted marketing text only; no real underwriting.
4. **Screenshot/email ingestion** ("send screenshot to info@…") — spoken instruction only; no inbound email/image processing.

(Note: real call transfer is now IN scope via your existing endpoint — D3 — pending the endpoint spec in §13.)

---

## 13. Still needed from you (to start / finish the build)

§0 decisions are now CONFIRMED. Remaining inputs:

1. **Transfer endpoint spec (D3)** — URL, method, auth, and the **call identifier** it expects (stream_sid / phone / Asterisk channel / call id). Determines whether the brain or go-server makes the call. *(Build proceeds with a transfer stub until this arrives.)*
2. **`agent_id` value** the dialer will send for this campaign (e.g. `salary-on-time`). One agent + auto section-select is assumed.
3. **Field names/units** from your panel: currency in whole rupees vs paise; date format (ISO `YYYY-MM-DD` assumed).
4. **Supabase** (for the prod read path, later): connection details / which project + table. Not needed for the testing phase (local Postgres seed).

---

## 14. File-by-file change list (for when we build)

**collections (`salary_on_time` branch)**
- `migrations/002_salary_on_time_fields.sql` (new)
- `scripts/seed_salary_on_time.sql` (new)
- `app/config.py` + `app/compliance_defaults.py` — `salary_on_time` TenantConfig + gate flags
- `app/engine/gate.py` — wrap pressure/disclosure checks behind flags (keep safety)
- `app/ws/borrower_context.py` — new fields → slots
- `app/memory/postgres_borrowers.py` — SELECT/map new columns
- `app/engine/tracker.py` — hydrate new slots
- `app/engine/actions.py` — `select_section`, `send_whatsapp_message`, `request_transfer`
- `app/clients/tools_sim.py` — sim for whatsapp/transfer
- `app/flows/salary_on_time/*.yml` — the flow pack (identity, third-party, offer×3, push, commit, close, objections)
- `app/ws/routing.py` — `salary-on-time` agent → tenant + entry flow
- `tests/sim/sot_*.json` + golden/unit tests

**websocket (`salary_on_time` branch)**
- `internal/brain/contract.go` — extend `BorrowerContextPayload` (optional inline fields)
- transfer endpoint: only needed here **if** the endpoint requires the live Asterisk channel (then go-server calls it on a `transfer` disposition). If it accepts `stream_sid`/phone, the brain calls it directly and no go-server change is needed. Decided by the §13.1 spec.
- (later) live WhatsApp — out of scope this phase

---

*Prepared on branch `salary_on_time`. No engine code changed yet — this is the spec to build against once §0/§13 are confirmed.*
