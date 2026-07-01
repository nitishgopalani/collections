# Conversation Repair Layer — Implementation Plan

**Goal:** Close the gap between our engine and Rasa Pro/CALM's proven "conversation
repair" behaviours, so the voice UX stays intact (no infinite loops, no identical
repeats, graceful escalation) — **without** switching platforms.

**Context:** Our architecture is already CALM-equivalent (Groq command-gen → YAML
flows + stack + compliance gate). The features Rasa is better at are all in the
**repair layer** (retry-cap, rephrase-on-repeat, correction, clarification,
silence, slot validation) — and in Rasa those live in **Rasa Pro (paid)**. We will
re-build them as a small, reusable, tenant-agnostic layer in our own engine so they
cover Pre-Closure **and** the upcoming On-Due / Post-Due flows automatically.

**Status:** APPROVED — building Phases 1, 2, 4 (engine-side). Phase 3 (voice/Go)
deferred as a future phase. Decisions locked in Section 7.

---

## 1. Feature gap: what Rasa has that we don't

| # | Rasa capability | Rasa mechanism | Our current state | Severity |
|---|---|---|---|---|
| F1 | Retry cap + graceful escalation | `pattern_clarification` counter → `pattern_human_handoff` | Only a within-turn 256-step guard (`executor.py`); **no cross-turn re-ask cap** → infinite loops | 🔴 Critical |
| F2 | Rephrase / vary on repeat | Contextual Response Rephraser, `pattern_repeat_bot_messages` | Variant rotation exists (`nlg.render_resolved` uses `state.attempts`) but SOT prompts have **1 variant each** → identical repeats | 🔴 Critical (UX) |
| F3 | Correction pattern | `pattern_correction` (update slot + confirm) | Patched only at final-confirm (`_coerce_sot_confirm`) | 🟠 High |
| F4 | Declarative slot validation | `validate_<slot>` / `collect.rejections` | Ad-hoc classifiers/coercions in `actions.py` | 🟠 High |
| F5 | Two-stage fallback (low confidence) | affirm → rephrase → escalate | Plain re-ask on `clarify` | 🟡 Medium |
| F6 | Clarification (ambiguous intent) | `pattern_clarification` ("did you mean X or Y?") | Pick top KB candidate / clarify | 🟡 Medium |
| F7 | User-silence handling (voice) | `pattern_user_silence` + `silence_timeout` → "are you there?" → escalate | None (endpointing only) | 🟠 High (voice) |
| F8 | Minimum pacing between bot messages | voice-stream pacing (1–2s gaps) | None explicit | 🟢 Low |
| F9 | Testing / inspector tooling | Rasa Inspector, test conversations, NLU eval | `turn_decision` logs + `sim` runner | 🟡 Medium |

---

## 2. Design principles (borrowed from Rasa)

1. **Repair is a cross-cutting layer, not per-branch code.** The happy path stays
   clean; robustness comes from generic behaviours applied to *every* `collect` step.
2. **Never dead-end, never loop forever.** Every step has a bounded number of
   attempts, then escalates to a human.
3. **Never repeat verbatim.** A re-ask must sound different (variant → simpler →
   escalate), because identical repeats are the #1 cause of caller frustration.
4. **Tenant-agnostic + config-driven.** Thresholds and phrasings live in
   `TenantConfig` / YAML so SOT, On-Due, Post-Due, and future tenants inherit them.
5. **Latency-first.** Prefer pre-authored variants (cacheable, 0 added latency) over
   live LLM rephrasing on the hot path.

---

## 3. Phased delivery

### Phase 1 — Repair core (biggest UX win, tenant-agnostic)

**F1 — Retry cap + escalation**
- Add `repair_counts: dict[slot, int]` to conversation state (persisted in slots).
- In `turn.py`, after the executor yields `question_slot`: if it equals the previous
  turn's `last_question_slot` **and** the slot is still unfilled → increment the
  slot's repair count; else reset it.
- New `TenantConfig.max_slot_retries` (default **2**).
- On exceed: **schedule a callback / log for manual follow-up** (no live transfer
  yet — see Decision Q1), speak a polite hand-off line, set disposition
  `ESCALATED_UNCLEAR`, reset the counter, and end the call gracefully.
- Emit the counter in `turn_decision` logs for observability.

**F2 — Rephrase / vary on repeat**
- Drive NLG `rotation_index` by the **per-slot repair count** (not global
  `state.attempts`), so each re-ask advances the variant.
- Author **2–3 escalating phrasings** for each key collect prompt:
  attempt 1 = normal; attempt 2 = simpler + example ("jaise shaam 6 baje");
  attempt 3 = "main aapko agent se jod deta hun".
- Reuse the existing `repeat_reply_id` path; no new hot-path LLM call.

**F3 — Correction re-route (generalised)**
- Generalise `_coerce_sot_confirm` into a config-driven **correction map**: per flow,
  declare which upstream slots are "correctable" and the step to re-enter.
- When awaiting slot X but the caller restates an already-committed slot Y
  (e.g. day/time change) → update Y, re-run the relevant classifier, jump to the
  correct branch, and (optionally) confirm the change ("theek hai, {new} kar dete hain").

**Deliverables:** engine changes in `turn.py`, `nlg.py`, `actions.py`, `config.py`;
new authored variants in `pre_closure.yml`; unit + sim tests.

### Phase 2 — Comprehension robustness

**F4 — Declarative slot validation**
- Add optional `validate:` / `rejections:` to `collect` steps in flow YAML (Rasa-style).
- Small validator registry: `time`, `date`, `enum`, `numeric_range`, `regex`.
- On reject: don't fill the slot, serve a **targeted** re-ask, increment repair count
  (feeds F1/F2). Removes the "day given where a time is expected" class entirely.

**F5 — Two-stage fallback**
- When command-gen returns `clarify` for a slot ≥ N times: switch to an affirmation
  ("aap ye kehna chahte hain — {top_candidate}? haan ya na"), then escalate.

**F6 — Clarification (ambiguous intent)**
- When ≥2 candidate flows fall within a confidence band, ask a 2-option
  disambiguation instead of guessing.

### Phase 3 — Voice UX (Go media server, `Websocket/`)

**F7 — User-silence handling**
- Add `silence_timeout`; on timeout play "kya aap sun paa rahe hain?" (repeat once),
  count consecutive silences, escalate/hang up after N.

**F8 — Minimum pacing between bot messages**
- Enforce a short gap between consecutive bot audio segments so replies don't rush.

### Phase 4 — Tooling & regression safety

**F9 — Inspector + unhappy-path test harness**
- Extend the `sim` runner to assert **no step is asked >max_slot_retries** and every
  conversation terminates (close or escalate).
- Add golden "unhappy path" conversations (day-change, repeated silence, gibberish,
  repeated already-paid) to CI.
- Lightweight decision inspector view over existing `turn_decision` logs.

---

## 4. Effort & sequencing (rough)

| Phase | Scope | Effort | Depends on |
|---|---|---|---|
| 1 | Repair core (F1–F3) | ~2–3 days | Open Qs Q1, Q2, Q3 |
| 2 | Comprehension (F4–F6) | ~3–4 days | Phase 1 |
| 3 | Voice UX (F7–F8) | ~2–3 days | Go server access (Q4) |
| 4 | Tooling (F9) | ~2 days | Phase 1 |

Recommend shipping **Phase 1** first, deploy, test a live call, then proceed.

---

## 5. Acceptance criteria

- No conversation re-asks the same slot more than `max_slot_retries` times; on exceed
  it escalates to a human with a clear disposition.
- A re-ask is **never** the identical wording of the previous prompt.
- A mid-flow change ("aaj nahi kal", "6 nahi 8 baje") updates the commitment and
  continues — no loop.
- A day/gibberish answer at a time step is rejected with a targeted re-ask, not stored.
- (Phase 3) Repeated silence ends with a polite check then graceful hang-up/escalation.
- All of the above hold for Pre-Closure **and** On-Due / Post-Due (tenant-agnostic).

---

## 6. Risks & mitigations

- **Over-eager escalation** (caller loses the bot too fast) → tune threshold (default 2),
  make it per-tenant; log every escalation for review.
- **Rephrase latency/cost** if we use live LLM → mitigate by using authored variants on
  the hot path; LLM rephrase only as an optional, cached, off-path enhancement.
- **Correction false positives** (treating a confirmation as a change) → keep the
  negation-aware logic; confirm the change before committing when ambiguous.
- **Go server ownership** — silence/pacing changes may touch code partly managed
  externally (see Q4).

---

## 7. Decisions (locked)

- **Q1 — Escalation target → CALLBACK.** On give-up, schedule a callback / log for
  manual follow-up and end gracefully. No live SIP transfer yet (wire later).
- **Q2 — Retry threshold → 2.** Re-ask the same question at most twice, then escalate.
- **Q3 — Rephrase approach → AUTHORED VARIANTS.** Pre-authored Hindi variants on the
  hot path (fast, cacheable, zero added latency). LLM rephrase optional/off-path later.
- **Q4 — Go media server (Phase 3) → DEFERRED.** Silence/pacing to be done in a future
  Phase 3; engine-side repair (Phases 1, 2, 4) first.
- **Q5 — Rollout scope → GENERIC.** Build tenant-agnostic so On-Due/Post-Due inherit it.
- **Q6 — Phrasing sign-off.** New Hindi re-ask/escalation lines to be reviewable via
  `flow_editor.html`.

---

## 8. Tracker

| Item | Phase | Status | Notes |
|---|---|---|---|
| F1 Retry cap + callback escalate | 1 | ✅ Done | threshold=2, callback; `robustness.track_slot_reask` + `turn.py`; tests green |
| F2 Rephrase-on-repeat (authored) | 1 | ✅ Done | per-slot rotation (`nlg._slot_reask_rotation`) + 3 authored Hindi variants; tests green |
| F3 Correction re-route | 1 | ⬜ Next | generalise confirm coercion; pair with F4 validation (executor re-entry) |
| F4 Slot validation | 2 | ⬜ Not started | |
| F5 Two-stage fallback | 2 | ⬜ Not started | |
| F6 Clarification | 2 | ⬜ Not started | |
| F7 Silence handling | 3 (deferred) | ⬜ Future | Go server |
| F8 Message pacing | 3 (deferred) | ⬜ Future | Go server |
| F9 Inspector + tests | 4 | ⬜ Not started | |
