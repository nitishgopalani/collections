# Salary-On-Time — On-Due & Post-Due Implementation Plan & Tracker

> Source of truth for the scripts: `Collection/SOT_FLOW.md` (sections **## On Due** and **## Post Due**).
> Reference implementation already shipped: **Pre-Closure** = `app/flows/salary_on_time/pre_closure.yml`.
> This document is both the **plan** and the **live tracker** — tick the checkboxes as each item lands.

---

## 0. TL;DR

- The three scripts (Pre-Closure / On-Due / Post-Due) share an **identical Step 1 (identity + Branch C third-party logic)** and an **identical Step 5 (closing)**. Only **Step 2 (purpose), Step 3 (pushes) and a few Step-4 confirm lines** differ.
- The commit engine (`classify_sot_commit_timing`) and all safety nets (read-only slots, objection suppression, single-`rupaye` formatter, third-person mode) are **already correct and reusable as-is**. No engine rewrite needed.
- Recommended build approach: **one `salary_on_time` tenant**, a shared opener, and a **due-date dispatcher** that branches into Pre / On / Post Step-2 subflows. Lower-risk fallback: three independent flow packs.

---

## 1. What is already done (reuse, do not rebuild)

| Capability | Where | Status |
|---|---|---|
| Identity + Branch C (C1/C2/C3) | `pre_closure.yml` `sot_opener` + relation classify | ✅ reuse verbatim |
| Commit timing classifier (ISO → enum) | `actions.py` `_classify_sot_commit_timing` | ✅ works for all 3 (see §3) |
| Read-only financial slots (no LLM overwrite) | `command_gen.py` `READ_ONLY_LLM_SLOTS` | ✅ reuse |
| Objection suppression while collecting a commit slot | `turn.py` (`sot_obj_*` filter) | ✅ reuse |
| Single "rupaye" spoken form | `nlg.py` `spoken_form_value` | ✅ reuse — **templates must NOT write "rupaye" after an amount placeholder** |
| Third-person (`third_person_mode`) confirm variants | `pre_closure.yml` `*_tp` templates | ✅ reuse pattern |
| Simulated tools: `send_whatsapp_message`, `transfer_call`, `hangup_call` | `actions.py` | ✅ reuse |

> **Carry-forward lesson (the "300 rupaye rupaye" bug):** `spoken_form_value` renders any integer amount as `"… rupaye"`. Every On-Due / Post-Due template that prints `{repay_amount}`, `{offer_amount}`, `{discount_amount}`, `{loan_amount}`, `{enhanced_limit}` **must omit a trailing literal "rupaye"**.

---

## 2. Script differences at a glance

| | Pre-Closure (live) | On-Due (build) | Post-Due (build) |
|---|---|---|---|
| **When** | `due_date` in future | `due_date == today` | `due_date < today` |
| **Step 2 hook** | "due date {due_date} hai … aaj pay karein to {discount_amount} save" | "due date **aaj** hai, payment receive nahi hua — **aaj kis time tak** karenge?" | "due date **nikal gayi**, penalty lag rahi hai — **kab tak** karenge?" |
| **Discount/offer angle** | Yes (save discount) | Penalty-from-tomorrow + 20% reloan | Penalty-already-accruing + 20% reloan |
| **# of pushes** | 2 (push → second push) | 3 (push → loan-benefit → cibil) then "kab tak?" | 4 (daily-increase+cibil → loan-benefit → cibil → **3.4 penalty angle**) |
| **Commit branches** | today / tomorrow / before_due / on_due / after_due | today / tomorrow / after_due→transfer | today / tomorrow / after_due→transfer |
| **After-due commitment** | warn → (transfer) | **transfer_call** | **transfer_call** |
| **Closing (Step 5)** | WhatsApp → hangup → closing line | identical | identical |

---

## 3. Why the commit classifier needs NO change

`_classify_sot_commit_timing` maps the LLM's ISO date to an enum:

- `committed <= today` → **today**  (always the best outcome — true for all three scripts)
- `committed == today + 1` → **tomorrow**
- `committed < due` → before_due · `== due` → on_due · `> due` → **after_due** (transfer)

For **On-Due** (`due == today`) and **Post-Due** (`due < today`), paying "aaj" still resolves to **today**, and anything beyond tomorrow resolves to **after_due → transfer**. So On-Due / Post-Due commit flows only need the **today / tomorrow / after_due** branches; `before_due` / `on_due` simply never fire. ✅ No code change.

---

## 4. Recommended architecture

**Option A (recommended) — one tenant, due-date dispatch**

```
sot_opener  (identity + Branch C)         ← shared, already built
   └─ route_scenario (by due_date vs today)
        ├─ due_date  > today  → sot_offer_pre_closure   (existing)
        ├─ due_date == today  → sotod_step2  (On-Due pack)
        └─ due_date  < today  → sotpd_step2  (Post-Due pack)
```

- Pros: zero duplication of identity/Branch C, production-realistic (real borrowers carry a real `due_date`), single tenant/agent_id.
- Cons: requires a small refactor of `sot_opener` to add `route_scenario` after identity, and the three Step-2 entry flows must live in the same flow set.
- A `select_sot_scenario` action (compares `due_date` to `call_date`) sets a `sot_scenario` slot used by `route_scenario`.

**Option B (fallback) — three packs / three agent_ids**

- `on_due.yml` + `post_due.yml` each duplicate the opener; route by `agent_id` (`salary-on-time-ondue`, `salary-on-time-postdue`) like the current test routing in `app/ws/handler.py`.
- Pros: zero risk to the live pre-closure pack. Cons: identity/Branch C duplicated 3×.

> **Decision needed (see §8 Q1).** Default assumption for the tracker below = **Option A**.

---

## 5. New components to build

### 5.1 Flow packs
- [ ] `app/flows/salary_on_time/on_due.yml` — flows prefixed `sotod_*`
- [ ] `app/flows/salary_on_time/post_due.yml` — flows prefixed `sotpd_*`

**On-Due flows (`sotod_*`):**
- `sotod_step2` — reminder ("due date aaj hai") + ask time-today; routes 2.2A→commit, 2.2B→push, 2.2C→already-paid.
- `sotod_push` — push1 (penalty from tomorrow) → push2 (loan benefit) → push3 (cibil) → "kab tak?" → commit.
- reuse `sot_commit` style with branches **today / tomorrow / after_due**; after_due → `transfer_call`.
- objection subflows `sotod_obj_*` (see §5.3).

**Post-Due flows (`sotpd_*`):**
- `sotpd_step2` — reminder ("due date nikal gayi, penalty lag rahi hai") + ask "kab tak?"; 2.2A→commit, 2.2B(no/tomorrow/future)→push, 2.2C→already-paid.
- `sotpd_push` — push1 (amount increasing daily + cibil) → push2 (loan benefit) → push3/3.4 (penalty angle) → "kab tak?" → commit.
- commit branches **today / tomorrow / after_due**; after_due → `transfer_call`.
- objection subflows `sotpd_obj_*` including penalty-specific ones (see §5.3).

### 5.2 Reply templates (reply pack / manifest)
- [ ] Add On-Due reply ids (`sotod_*`) with Hindi (+ optional English) text, plus `*_tp` third-person variants for confirms.
- [ ] Add Post-Due reply ids (`sotpd_*`) likewise.
- [ ] **No literal "rupaye"** after amount placeholders (formatter adds it).
- [ ] Regenerate `reply_manifest.json` and update manifest-lock tests.

### 5.3 Objection coverage (from SOT_FLOW.md)
On-Due / Post-Due reuse most Pre-Closure objections; **new/changed** ones to add:
- [ ] On-Due: penalty-starts-tomorrow framing variants of `no_money`, `salary_not_in`.
- [ ] Post-Due penalty-specific:
  - "कितनी पेनल्टी लगी है?" (how much penalty)
  - "कितना पे करना है?" (total payable = `{repay_amount}`, incl. penalty)
  - "2 दिन बाद कितना लगेगा?" (amount in 2 days)
  - "टाइम नहीं है, बाद में call करना" → ask specific callback time
  - "बाद में पेमेंट करूँगा" (penalty-keeps-growing push)
- [ ] Shared objections (already in pre-closure, re-point text): already-paid, never-took-loan, wrong-amount, medical→hangup, job-loss, link request→WhatsApp, death-in-family→hangup, out-of-station, bot/AI, support-number, references-called→transfer, account-frozen→transfer, high-interest, reduce-amount, busy/callback, hold, "do whatever you want".

### 5.4 KB seeding
- [ ] Extend `scripts/seed_kb_flows.py` to glob `on_due.yml` + `post_due.yml` and enrich `sotod_obj_*` / `sotpd_obj_*` descriptions (mirror the SOT objection enrichment already done for pre-closure).
- [ ] Re-run `PYTHONPATH=. python scripts/seed_kb_flows.py` against the target DB.

### 5.5 LLM command hints
- [ ] Reuse `sot_payment_intent`, `sot_payment_intent_2`, `sot_commit_timing`, `sot_final_confirm` hints as-is.
- [ ] Add `sotod_*` / `sotpd_*` collect-slot hints only where wording diverges (e.g. Post-Due "kab tak" answer → `sot_commit_timing`).

### 5.6 Routing & test borrowers
- [ ] **Option A:** add `select_sot_scenario` action + `route_scenario` in opener; no new agent_id.
- [ ] **Option B:** map `salary-on-time-ondue` / `salary-on-time-postdue` agent_ids in `app/ws/handler.py`.
- [ ] Test borrowers in `app/memory/test_borrower.py`:
  - On-Due borrower: `due_date = today`.
  - Post-Due borrower: `due_date = today - 4` (penalty accruing).
  - Add `TEST_SOT_SCENARIO` env (or agent_id) to pick which borrower/scenario the bare-line test call uses.

### 5.7 Tenant config
- [ ] Confirm `salary_on_time` tenant flags (attempt caps already raised; compliance gate) apply to the new flows. No new tenant if Option A.

---

## 6. Phased delivery tracker

### Phase 0 — Decisions & scaffolding
- [ ] Q1 architecture decided (Option A vs B) — see §8
- [ ] Q2 English-line policy decided (Hindi-only vs bilingual templates)
- [ ] Branch created off `main`

### Phase 1 — On-Due
- [ ] `on_due.yml` flows (`sotod_step2`, `sotod_push`, commit branches, closing)
- [ ] On-Due reply templates (+ `_tp` variants), no double "rupaye"
- [ ] On-Due objection subflows
- [ ] KB seed + re-seed
- [ ] On-Due test borrower (`due_date = today`) + routing
- [ ] Unit tests: identity→commit happy path, push×3→commit, after_due→transfer, already-paid
- [ ] Local YAML/loader validation green
- [ ] Deploy to test server (`redeploy-brain`) + live call verification

### Phase 2 — Post-Due
- [ ] `post_due.yml` flows (`sotpd_step2`, `sotpd_push` with 4 pushes incl. penalty angle, commit, closing)
- [ ] Post-Due reply templates (+ `_tp`), no double "rupaye"
- [ ] Post-Due penalty-specific objection subflows
- [ ] KB seed + re-seed
- [ ] Post-Due test borrower (`due_date = today - 4`) + routing
- [ ] Unit tests: happy path, 4-push ladder, after_due→transfer, penalty-amount objections
- [ ] Local YAML/loader validation green
- [ ] Deploy + live call verification

### Phase 3 — Hardening & handoff
- [ ] Regenerate `reply_manifest.json`; manifest-lock tests pass
- [ ] Full deterministic test suite green
- [ ] Latency check parity with pre-closure
- [ ] Update `SALARY_ON_TIME_PROGRESS_TRACKER.md`
- [ ] Merge to `main`

---

## 7. Acceptance / test matrix (per script)

| Scenario | Expected | On-Due | Post-Due |
|---|---|---|---|
| Confirms identity, pays today + time | confirm line → Step 5 (WhatsApp→hangup) | ☐ | ☐ |
| Refuses, accepts after push | push ladder then commit | ☐ | ☐ |
| Commits tomorrow | tomorrow push → confirm | ☐ | ☐ |
| Commits day-after / future | `after_due` → `transfer_call` | ☐ | ☐ |
| "already paid" | screenshot/ref ask, no loop | ☐ | ☐ |
| Third-party family (C1) | third-person wording throughout | ☐ | ☐ |
| Cousin/friend (C3) | restricted, no amounts shared | ☐ | ☐ |
| Penalty amount question (Post-Due) | states `{repay_amount}`, pushes today | n/a | ☐ |
| No double "rupaye" anywhere | single "rupaye" | ☐ | ☐ |
| No commit-timing loop on date answers | one classify→route | ☐ | ☐ |

---

## 8. Decisions (resolved 2026-07-01)

- **Q1 → Option A (one tenant + due-date dispatch).** Platform is multi-tenant (any
  tenant can call); the `salary_on_time` tenant uses a `select_sot_scenario` action +
  `route_scenario` in the opener to branch Pre / On / Post by `due_date` vs `call_date`.
  No new agent_id needed. Repair-layer stays tenant-agnostic.
- **Q2 → Multi-language (Hindi + English).** Author bilingual templates using the
  existing language ladder; runtime default remains Hindi.
- **Q3 → Post-Due 4-push ladder confirmed.** Push the customer up to **4 times**
  (daily-increase+cibil → loan-benefit → cibil → penalty angle) before "kab tak?" →
  commit/transfer.
- **Q4 → Scaffolded (Model A), endpoint pending.** Live transfer is fully wired:
  flows speak the connect line → `transfer_call` declares intent → an async hook in
  `handle_turn` calls a swappable provider (`app/clients/transfer.py`). Default mode is
  `stub` (logs + returns `pending`, bot leg ends cleanly) so test calls behave sensibly
  today. When the telephony endpoint is available, set `TRANSFER_MODE=live`,
  `TRANSFER_ENDPOINT_URL`, `TRANSFER_AUTH_TOKEN` (and optionally
  `TRANSFER_DEFAULT_TARGET`) — no code change. Confirm with telephony: the call
  identifier the endpoint keys on (we pass `call_id` = WS `session_id`), the request/
  response contract (`_build_payload`), and whether the Go server needs a distinct
  "detach bot, keep customer" signal vs `end_call`.
- **Q5 → Env flag.** Use `TEST_SOT_SCENARIO=pre|on_due|post_due` to pick which test
  borrower/`due_date` the bare TEST_MODE line uses (see below). In production the real
  borrower's `due_date` drives `route_scenario` automatically — this flag is only a
  test convenience so you can exercise each script on the test line.

---

## 9. Notes / changelog
- _(2026-07-01)_ Plan created. Pre-closure on-due loop + duplicate-"rupaye" bugs fixed and deployed (collections `0097f25`) — patterns folded into §1 as reusable lessons.
