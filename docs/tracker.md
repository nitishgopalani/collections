# Collections Brain — Engine Upgrade & PaisaLo Onboarding Tracker

**Repo:** `nitishgopalani/collections` · **Base branch:** `feature/sot-restore-test` · **New branch:** `feature/tier23-engine-upgrade`
**Implementer:** Cursor · **Reviewer/Architect:** Claude (paste Cursor's diff summary + test output back into chat after every checkpoint)
**Rule:** NO commits, NO deploys without explicit sign-off at each ✋ CHECKPOINT. One phase = one review cycle.
**Out of scope:** DTMF/IVR layer (already live separately). Compliance copy decisions for PaisaLo legal-threat lines (business call, tracked separately as D-1).

---

## Status Legend
`[ ]` not started · `[~]` in progress · `[R]` in review · `[x]` signed off

## Phase Map (dependency order)

| Phase | What | Why first/next | Est. |
|---|---|---|---|
| P0 | Flow-set caching + refusal regex + dead-cue cleanup | Zero-risk latency + fixes live call bug; unblocks clean measurement for P2 | 0.5 day |
| P1 | Tenant-generic provider refactor | Everything after this must NOT hardcode `salary_on_time`; do it before writing more logic | 1.5 days |
| P2 | Tier 2 — full-catalog command routing (RAG demoted) | Kills routing misses; prerequisite for measuring Tier 3 correctly | 1 day |
| P3 | Tier 3 — `respond` command: answer + resume | The PaisaLo "out-of-context question" requirement; biggest UX unlock | 1.5 days |
| P4 | Attempt-indexed objection escalation (1st/2nd responses) | PaisaLo script requirement; small, isolated | 0.5 day |
| P5 | PaisaLo tenant build (6 scenarios, NPA resolution ladder) | Pure content + config once P1–P4 land | 2 days |
| P6 | Golden regression + replay eval harness | Ship gate | 1 day |

---

# PHASE 0 — Hygiene: caching, refusal regex, dead cues

### P0.1 `[x]` Flow-set caching
- `app/engine/command_gen.py`: replace `load_all_flows` import with `get_flow_set`; swap all call sites in `known_slot_names()`, `known_flow_names()`, `_active_flow_slot_hints()`.
- `app/engine/retrieval.py` → `resolve_flows()`: when `flows_dir is None` use `get_flow_set()`; keep `load_all_flows(flows_dir)` only for explicit test override.
- New test `tests/unit/test_flowset_caching.py`: monkeypatch `app.flows.loader.load_all_flows` with a counter; one full `handle_turn` on salary_on_time must show counter == 0.

### P0.2 `[x]` Soft-refusal inability regex
- `app/engine/turn.py` (~line 489): delete corrupted cue `"नहीं हो पायegi"`; dedupe cues that exist in both `_SOT_REFUSAL_CUES` and `_SOT_INTENT_REFUSAL_CUES`.
- Add module-level:
  ```python
  _SOT_INABILITY_RE = re.compile(
      r"(नहीं|नही|नहि|\bnahi\b|\bnahin\b|\bnhi\b|\bno\b)"
      r".{0,30}?"
      r"(पा[एऊउ]|सक[तनू]|paung|payeg|paeg|sakt|sakun|can'?t|cannot|unable|not able)",
      re.IGNORECASE | re.UNICODE | re.DOTALL,
  )
  ```
- In `_coerce_sot_payment_refusal`: fire on `cue-match OR regex-match`. All other guards unchanged (esp. the non-empty-LLM-value early return).
- Tests — MUST fire → refused: "नहीं, आज तो नहीं आ पाएगी" / "नहीं नहीं आज नहीं कर पाऊंगा" / "abhi nahi ho paega" / "sorry main aaj pay nahi kar sakta". MUST NOT fire: "नहीं नहीं, मैं कर दूंगा" / "haan kal kar dunga". Verify-and-report: "nahi aaj nahi kal karunga".

### P0.3 `[x]` Routing-miss must not burn borrower retries
- `robustness.track_slot_reask`: add kwarg `routing_miss: bool = False`; when True skip increment + skip escalate check, keep slot-changed reset branch.
- `turn.py`: pass `routing_miss=weak_jump_suppressed` (and later `respond_fired`, see P3).
- Test: 3 consecutive unroutable borrower questions at `sot_payment_problem` → NO `ESCALATED_UNCLEAR`.

**✋ CHECKPOINT 0** — SIGNED OFF (CP0 + R1–R7).

---

# PHASE 1 — Tenant-generic provider refactor

**Goal:** engine has ZERO `tenant_id == "salary_on_time"` string checks. All tenant behaviour comes from a declarative **TenantRuntimeProfile**. PaisaLo becomes config, not code.

### P1.1 `[x]` Define `TenantRuntimeProfile`
New `app/engine/tenant_profile.py` (pydantic model + registry):
```python
class TenantRuntimeProfile(BaseModel):
    tenant_id: str
    flow_prefix: str                    # "sot_" | "plo_"
    onrails_flows: frozenset[str]
    commit_collect_slots: frozenset[str]
    push_intent_slots: frozenset[str]
    reversal_slots: frozenset[str]
    main_ladder_prefixes: tuple[str, ...]
    blocked_commands: frozenset[str]
    pinned_flows: list[str]
    dispute_flows: list[str]
    coercion_chain: list[str]           # ordered names, e.g. ["dispute","amount_query","willing","refusal","identity","reversal","confirm","link"]
    cue_packs: dict[str, list[str]]     # named cue lists (refusal, willing, dispute themes...) — loaded from YAML
    respond_enabled: bool = False       # Tier 3 switch (P3)
    scenario_selector: str = "due_date" # "due_date" | "dpd_bucket_npa" (P5)
    frustration_escalate_turns: int = 3
```
- Registry: `get_tenant_profile(tenant_id) -> TenantRuntimeProfile | None`; SOT profile constructed from the CURRENT hardcoded constants (behaviour-identical), loaded from `app/tenants/salary_on_time.yml`.
- Cue packs move from Python tuples to the tenant YAML (`cue_packs: {intent_refusal: [...], willing: [...], willing_disqualifiers: [...], dispute_never_loan: [...], ...}`). The inability regex stays in code (shared, language-level, not tenant-level).

### P1.2 `[x]` Refactor `turn.py` to consume the profile
- Every `request.tenant_id == "salary_on_time"` branch → `profile = get_tenant_profile(request.tenant_id)`; `if profile is not None:` gates the whole scripted-tenant block.
- `_coerce_sot_*` functions become generic `_coerce_*` taking cue lists/slot sets from the profile (rename module section "scripted-tenant coercions"). Coercion chain executes in `profile.coercion_chain` order with the existing "first-fired wins" semantics.
- `SOT_*` module constants remain ONLY inside `app/tenants/salary_on_time.yml` (single source of truth).
- `config.py`: `tenant_config("salary_on_time")` special-case stays for now (compliance knobs), but add TODO marker; runtime routing behaviour must come from the profile only.

### P1.3 `[x]` Regression proof
- Full unit + golden suite green with SOT profile — **zero behaviour diff expected**. Any diff = bug, report it.
- New test: fabricated tenant `test_generic` with a 3-flow YAML profile completes a scripted happy path with no code edits.

**✋ CHECKPOINT 1** — SIGNED OFF.

---

# PHASE 2 — Tier 2: full-catalog routing (RAG demoted)

**Goal:** for scripted tenants, the LLM sees the WHOLE tenant flow catalog (name + description), not just KB hits. Retrieval stays only for open/default tenants.

### P2.1 `[x]` Catalog builder
- New `app/engine/catalog.py`: `tenant_flow_catalog(profile, flows) -> list[dict]` — all flows matching `profile.flow_prefix` **or** `main_ladder_prefixes` (covers `sotod_`/`sotpd_`), each `{name, description}` (no score key). Cache per `(tenant_id, flow_set_version)`; invalidate on `reload_flow_set` via `bump_flow_set_version`.
- Compress descriptions: first sentence + trigger phrases truncated to ~25 words each.
- Log token estimate (`len(json)/4`) once per tenant. Target ≤ 4.5k.

### P2.2 `[x]` Wire into `handle_turn`
- Scripted tenant + call open → `tenant_flow_catalog(...)`; **skip retrieval** (catalog mode). Legacy digression/pin/floor retained only behind `SCRIPTED_CATALOG_ROUTING=false`. Non-profile RAG path untouched.
- `_clarify_if_ambiguous` (F6): skip when candidates carry no scores.
- Dispute accumulator kept (matcher OR LLM-proposed start_flow).
- Deflection objections filtered from catalog while awaiting `commit_collect_slots`.
- `parse_and_validate_commands`: reject `start_flow` not in the non-empty candidate/catalog list.

### P2.3 `[x]` Prompt guardrail
- `build_user_prompt(..., catalog_mode=True)` adds `routing_note` (complete catalog; prefer set_slot).

### P2.4 `[x]` Tests
- `tests/unit/test_catalog.py` — cache invalidation, tokens, deflection filter.
- `tests/golden/test_catalog_routing.py` — never_loan turn-1, kaise-pay without pinning, busy excluded at intent_2.
- Groq latency: `scripts/_p2_groq_latency.py` (3×5-turn golden).

**✋ CHECKPOINT 2** — SIGNED OFF (D-3/D-5).

---

# PHASE 3 — Tier 3: `respond` command (answer + resume)

**Goal:** PaisaLo's rule, engine-level: *pehle ek vaakya mein jawab, phir script par wapas.* Bounded generation, gated, always resumes the pending collect question.

### P3.1 `[x]` Command schema
- Add `"respond"` to `VALID_COMMANDS`; `Command` gets optional `text: str`.
- Validation in `parse_and_validate_commands`: `respond` accepted only if `profile.respond_enabled`; text non-empty, ≤ 220 chars; strip newlines/markdown. A `respond` may co-exist with `set_slot`, never with `start_flow` (drop the `respond` if both).
- `RESPOND_MAX_CHARS=220` single constant in `command_gen.py` (D-2 still OPEN).

### P3.2 `[x]` Prompt contract
- System prompt (catalog mode) adds: "If the borrower asks a question no flow covers, output {command:'respond', text:'<ONE short Devanagari sentence answering ONLY from the facts in slots>'}. NEVER invent amounts, dates, waivers, penalties, or policies. If the answer is not in slots, respond with the tenant's unknown-info line verbatim: '<profile.unknown_info_reply>'."
- Add `unknown_info_reply` field to TenantRuntimeProfile; `respond_enabled: true` for salary_on_time + test_generic.

### P3.3 `[x]` Engine handling
- In `handle_turn`, after coercions/validation: if a `respond` survived →
  1. `respond_text` held aside; do NOT push any flow frame.
  2. NLG builds reply = `respond_text + " " + re_utterance_of_pending_collect` (short retry via `render_short_reask` / `sot_push_retry`, not the full offer).
  3. **Fact-grounding check (hard):** every numeric token (₹, digits, date-like) in respond_text must appear in hydrated slot VALUES (normalized containment); else replace whole respond_text with `unknown_info_reply`. `app/engine/respond_guard.py`.
  4. Compliance gate runs on the COMBINED final text — proven by golden (poison only in re-ask → block).
  5. `track_slot_reask(..., routing_miss=True)` when `respond_fired`.
  6. Audit: `guards.respond_fired`, `grounding_result` (pass|swapped), `final_text_len`.

### P3.4 `[x]` Tests
- "kitni payment due hai?" mid-push (pre_closure) → single sentence containing `repay_amount` + re-ask, NO penalty wording, flow_stack unchanged, no retry burned.
- "office kahan hai aapka?" (not in slots) → `unknown_info_reply` + re-ask.
- Adversarial: LLM respond text containing an invented "₹500 waiver" → guard swaps to unknown_info_reply (unit-test the guard directly).
- Gate: prohibited phrase in COMBINED (respond + re-ask) → gate blocks, safe fallback plays.
- Full simulated session 5f001c27 t1–t7 → `scripts/_p3_session_5f001c27_transcript.txt`.
- Also: `sot_obj_pay_later_today` transfer/hangup removed (on-ladder only).

**✋ CHECKPOINT 3** — SIGNED OFF.

### Checkpoint-3 clarifications (C1–C3)
- **C1:** Mid-push respond turn (test t3 / session t5) `guards` log — see CHECKPOINT 4 report body / `scripts/_p3_c1_guard_line.json`.
- **C2:** Reason-given after respond covered by new golden `test_reason_given_after_respond_advances_push` (extends salary_delay path from `test_routing_miss_out_of_catalog_does_not_escalate`).
- **C3:** Risk **R5** logged below; `amount_paid` / `last_payment_*` added to `FACT_SLOTS_FOR_RESPOND` when hydrated; prompt contract notes `'<N> rupaye'` digit form.

---

# PHASE 4 — Attempt-indexed objection escalation

### P4.1 `[x]` Mechanism
- `ResponseTemplate` gains optional `attempt: int` (1-based). NLG variant selection: count prior utterances of this reply_id in this call (persist per-reply counter in slots `_reply_counts`), pick the template with matching `attempt`, fall back to highest defined. Deterministic — replaces random variant pick ONLY when any template in the group has `attempt` set.
- Cap: after the highest-numbered attempt has played once more, executor follows the flow's `escalate_to` step if defined (e.g. END_CALL / branch-referral), via a new optional flow-step field `escalate_to`.

### P4.2 `[x]` Tests
- Two-attempt group plays 1st then 2nd then holds at 2nd / escalates per `escalate_to`.
- Reply counters cleared on `sot_call_closed`.

**✋ CHECKPOINT 4** — SIGNED OFF (CP4, D-1).

### CP4 close-out (C1 / V1 / V2) `[x]`
- **C1:** `refusal_matched_via: cue|regex` on turn guards; t3 soft-refuse log in `scripts/_p4_c1_t3_refusal_guards.json`.
- **V1:** `test_build_user_prompt_excludes_reply_counts_and_underscore_slots` — `_reply_counts` / `_`-prefixed slots absent from `build_user_prompt`.
- **V2:** `test_objection_attempt_one_two_then_escalate_end_call` + transcript `scripts/_p4_v2_attempt_escalation_transcript.txt`.

### P4.5 `[x]` Tenant gate allowlist (D-1 option c)
- `TenantRuntimeProfile.gate_allowlisted_phrases: list[str]` (default `[]`).
- Before blocking on collection-pressure, if a word-bounded allowlisted phrase fully covers the matched pressure span → log `gate_warnings` with `"allowlisted": true`, do **not** block/swap.
- Non-allowlisted pressure behaves as today. Prohibited-phrase path unchanged.
- `salary_on_time.yml`: empty allowlist. `paisalo.yml` (P5) gets approved phrases at copy review.
- Tests: `tests/unit/test_gate_allowlist.py`.

**STOP** — Phase 5 starts only after P4.5 + C1/V1/V2 are all signed off.

---

# PHASE 5 — PaisaLo tenant build

### P5.1 `[x]` Tenant profile + scenario selector
- `app/tenants/paisalo.yml`: flow_prefix `plo_`, respond_enabled: true, unknown_info_reply from script ("माफ़ कीजिए... {{branch}} ब्रांच या पैसालो हेल्पलाइन से संपर्क करें"), scenario_selector: `dpd_bucket_npa`.
- New selector action `select_plo_scenario`: inputs `dpd`, `npa_flag`, `product` (ABF|MFI) → one of `predue | ondue | postdue1 | postdue2 | postdue3 | npa`. Persona/tone (Neha/Anjali/Arjun/Aman) is a per-scenario TTS-voice + copy concern, not engine logic — voice id goes in scenario slot for the connector.
- Borrower hydration: add `days_past_due`, `branch`, `branch_address`, `last_date_paid`, `product`, `npa_flag` to FACT_SLOTS + hydration path.

### P5.2 `[x]` Flow YAMLs (`app/flows/paisalo/`)
- `predue.yml`, `ondue.yml`, `postdue.yml` (3 severity ladders sharing objection flows, attempt-indexed where script defines 1st/2nd), `npa.yml`.
- **NPA is a RESOLUTION ladder, not a push ladder:** greeting → 2-min consent → NPA disclosure → timeline ask → (refuse×2 → branch referral + END_CALL). Model per script exactly; commit chain asks for timeline/partial, never "aaj hi karo".
- All ~20 objections per scenario from the PDF, verbatim copy, `{{...}}` → `{...}` slot syntax mapped (`amount`→`repay_amount` mapping table in the YAML header comment).
- END_CALL markers → existing hangup action; "After capturing the callback time: END_CALL" → collect `callback_window` then hangup.
- Deterministic cue packs in paisalo.yml: loan-denial, dealer-payment, waiver-political, already-paid, new-loan, which-EMI (reuse SOT Hindi cue style; dealer/waiver are PaisaLo-specific).

### P5.3 `[x]` Validation
- `validate_flow_set` passes; every `utter:` id has a response; every collect slot reachable; no orphan `escalate_to`.
- Scripted walkthrough tests: one golden per scenario bucket (6), plus NPA refuse-twice → branch-referral close, plus out-of-context question in NPA (script's own example) via Tier 3.

### P5.4 `[x]` Hindi pressure lexicon (D-1 restore)
- `COLLECTION_PRESSURE_PHRASES` gains tenant-agnostic Hindi legal/NPA family; `normalize` nukta-insensitive.
- Re-derive `paisalo.yml` `gate_allowlisted_phrases` from actual hits; do **not** allowlist `emi`.
- CP5 F1: standalone `"emi"` removed from pressure lexicon.

**✋ CHECKPOINT 5** — SIGNED OFF (F1–F3).

---

# PHASE 6 — Regression + replay eval

### P6.0 Known-red baseline (Checkpoint-0 C1 / remediation R1)
Pre-existing on `664e42f` (P0+P1 stashed). Full paste: `scripts/_r1_base_failures.txt`. Summary:

| Test | Failure |
|---|---|
| `test_link_request_confirms_receipt_then_hangs_up` | got `sot_ask_time`, expected `sot_link_thanks_close` |
| `test_link_request_not_received_resends_reassures_then_hangs_up` | got `sot_push_retry`, expected `sot_link_retry_wait` |
| `test_link_request_not_received_llm_boolean_still_resends` | got `sot_offer_pre_closure`, expected `sot_link_retry_wait` |
| `test_brain_ws_turn_emits_chunk_flow_class_done` | gate `silent:outside_call_window` → no `chunk` |
| `test_sim_script_runs[dynamic_hardship]` | `hardship_reason` stayed `None` |
| `test_dynamic_ptp_sim_emits_turn_decision_logs` | `WindowsPath` has no `.get` in `run_sim_script` |

**P6 fix sub-item — link-receipt goldens:** repair the 3 `test_link_request_*` goldens (receipt → thanks/close; not-received → resend/wait; LLM boolean `false` → same resend path). Do not treat as P0/P1 regressions.

### P6.1 `[x]` Replay harness
- `scripts/replay_eval.py` + `scripts/_p6_replay_seed.jsonl` — 27 turns, accuracy 100%, 0 info ESCALATED_UNCLEAR.
- Report: `scripts/_p6_replay_report.json`.

### P6.2 `[x]` Full suite
- P6.0 known-red cleared (6/6 green). Core unit+golden+compliance: 719 passed (excl. live_*).
- Remaining fails: WS/conference/prompt-mode `startup_validation` SystemExit — pre-existing env (see `_p6_f2_failures.txt`).
- Latency: `scripts/_p6_latency_table.md`.

**✋ FINAL CHECKPOINT** — SIGNED OFF. Seven squashed commits on `feature/tier23-engine-upgrade`. D-4 CLOSED (priya/neha/kabir/amit @ bulbul:v3 per-call). Do not merge to master until live pilot.

---

## CP-TEST / CP-TEST2 — matrix as expectation

| ID | Status | What |
|---|---|---|
| CP-TEST | `[x]` | Fixture replay + scenario matrix + stale-build header. First grid was a SNAPSHOT of StubLLM output. |
| CP-TEST2 | `[x]` | Hand-authored expects for late_fee / office / complaint / already_paid. Deterministic `match_fact_compose` (trigger_synonyms). 75/75 green. Non-NPA OOF compose coverage 20/20 (100%). WORKLOG #036. |

**✋ CP-TEST2** — SIGNED OFF this sitting. Do not start W5. Do not split DEBT-045.

| ID | Status | What |
|---|---|---|
| CP-UI6A | `[x]` | Flow Inspector: GET graph + catalog, Flows tab, Test live position. SIGNED OFF. |
| CP-UI6B-1 | `[x]` | Visual builder canvas + health overlay. Collect-without-YAML-escalate reclassified: implicit repair is amber, not red. Paisalo 0 err / 16 warn / 0 orphan. WORKLOG #038/#039. |
| CP-UI6B-2 | `[R]` | Drag/connect/undo + POST `/flow/validate` (dry, same health fn). No publish. WORKLOG #039. |
| CP-UI6B-3 | `[ ]` | Publish gate + revert. Not started. |

---

## Decisions Log (Nitish to close)
| ID | Decision | Status |
|---|---|---|
| D-1 | PaisaLo legal-threat / pressure copy — **tenant-level allowlist (option c)** via `gate_allowlisted_phrases`; allowlisted hits warn-only, else gate blocks as today | CLOSED (P4.5) |
| D-2 | Tier 3 respond max length 220 chars — ok for TTS pacing? | OPEN |
| D-3 | P2 latency budget: accept up to +150 ms command_gen for full catalog? | OPEN |
| D-4 | PaisaLo Sarvam voices per scenario (priya/neha/kabir/amit @ bulbul:v3 per-call; tts_pace 0.9/0.95 on postdue3/npa) | CLOSED |

## Risk Register
- **R1:** P1 refactor touches 1500+ lines of turn.py — mechanical but wide. Mitigation: behaviour-identical requirement + full golden suite before/after.
- **R2:** Full catalog may degrade Groq JSON reliability (longer prompt). Mitigation: P2.4 measures; fallback = compressed catalog.
- **R3:** Tier 3 hallucination. Mitigation: fact-grounding guard (P3.3.3) + gate + 220-char cap + unknown_info fallback.
- **R4:** PaisaLo copy has ASR-hostile long sentences (>40 words). Flag during P5 review; splitting is a copy decision, not engine.
- **R5:** Tier-3 “kitni payment di / already paid” vs due-amount confusion — LLM may answer `repay_amount` when `amount_paid`/`last_payment_*` are absent. Mitigation: expose those facts when hydrated; prompt forbids using `repay_amount` for paid-amount questions; unknown_info_reply when facts missing; grounding still blocks invented digits.