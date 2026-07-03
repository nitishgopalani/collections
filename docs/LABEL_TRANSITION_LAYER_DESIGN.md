# Label Transition Layer Design

> Second-round **design report only**. No application code is modified by this document.
> Companion to `Collection/docs/CURRENT_COLLECTION_ROUTING_STATUS.md`.
> Target codebase: `Collection/` (Python FastAPI brain). Line numbers reference code at time of writing.

## 1. Executive Summary

The Label Transition Layer (LTL) is a **thin, deterministic decision layer** that sits inside `handle_turn` **after** all existing command shaping (LLM command_gen → SOT coercions → slot validation → confidence floor → dispute accumulator) and **before** `tracker.apply`. It does not replace any existing mechanism; it reuses them.

Its job is to answer one question per turn:

> Given `previous_label`, the `current_label` implied by this turn's (already-shaped) commands/transcript, and the conversation state — what should happen to routing?

It emits one of nine **decisions** (continue / switch / resolve-and-switch / clarify / block-high-risk / escalate / keep-flag-allow-payment / decay / accumulate) and translates that decision into the **existing** command vocabulary (`start_flow`, `cancel_flow`, `set_slot`, `clarify`) plus a small amount of state bookkeeping (`active_label`, `previous_label`, `label_history`, `unresolved_high_risk_labels`, `evidence_by_label`).

Key properties:
- **Additive:** all new state lives in `ConversationState.slots` under reserved keys in Phase 1 (no schema migration), with an optional promotion to typed fields in a later phase.
- **Reuses routing primitives:** it never manipulates `flow_stack` directly — it produces commands that `tracker.apply` + `priority.reorder` already know how to execute.
- **Closes the two known gaps:** (a) fragile intent switching when `SOT_DIGRESSION` is off, by making high-value switches explicit and label-driven rather than KB-confidence-driven; (b) missed high-risk disputes, by persisting `unresolved_high_risk_labels` so a later "send link" is **blocked/clarified** even if the deterministic matcher missed the original phrasing.
- **Phased:** Phase 1 is rules-only (flow names + coercion outputs + transcript cues). No ML. Later phases add model-produced labels, a general evidence accumulator/decay, and a trained classifier.

## 2. Why Existing Code Is Not Enough

From `CURRENT_COLLECTION_ROUTING_STATUS.md` §5–§8:

| Existing mechanism | What it does | Why it is not a label transition layer |
|--------------------|--------------|-----------------------------------------|
| `tracker.apply` / `flow_stack` (`tracker.py` L78) | Push/pop/cancel flows | Stores *flows*, not *labels*; no notion of previous vs current intent |
| `priority.reorder` (`priority.py` L36) | Static priority ladder parks lower flows | Fixed ordering; cannot express "resolve refusal then allow payment" or "block link because dispute unresolved" |
| SOT coercions (`turn.py` L170–494) | Deterministic per-slot overrides | Single-turn, single-slot; no cross-turn label memory |
| Dispute accumulator (`turn.py` L621) | Cross-turn evidence for **disputes only** | Hard-coded to dispute themes; not general `evidence_by_label`; no decay |
| Frustration guard (`robustness.py` L107) | Anger streak → escalate | One signal; not a transition decision surface |
| Confidence floor / pinned (`turn.py` L557 / L529) | Suppress or force flow jumps by KB score | KB-score heuristic; disabled unless `SOT_DIGRESSION` on; no memory of what the borrower was doing before |
| Persona history (`persona.py` L462) | Records persona `from`/`to` | Only persona; not intent/risk; analytics-only, not routing |

Consequences visible in the report's Trace A/B:
- **Trace A (refusal → link):** switching depends on digression flag + KB candidate + LLM; fragile when digression off.
- **Trace B (dispute → link):** if the dispute matcher misses the phrasing and the accumulator has not reached the bar, a later "link bhej do" can send a link **over an unresolved dispute** — there is no persistent unresolved-risk state to block it.

The LTL adds exactly the missing piece: **an explicit, inspectable `(previous_label, current_label, state) → decision` step** that consumes the outputs of the existing layers and constrains the final commands.

---
## 3. Proposed Minimal Architecture

```
handle_turn (turn.py)
  ... LLM command_gen ...
  ... SOT coercions (L1246–1268) ...
  ... validate_commands (L1274) ...
  ... confidence floor (L1294) ...
  ... dispute accumulator (L1313–1329) ...
  ┌─────────────────────────────────────────────────────────────┐
  │ LABEL TRANSITION LAYER  (NEW, ~L1330, before tracker.apply)   │
  │                                                               │
  │  1. label_state   = load_label_state(state)                   │
  │  2. current_label = detect_current_labels(commands, awaiting, │
  │                        transcript, candidate_flows, matchers)  │
  │  3. label_state   = update_evidence(label_state, current_label)│
  │  4. decision      = transition_allowed(prev, current, state,   │
  │                        label_state)   # consults matrix        │
  │  5. commands      = build_transition_commands(decision,        │
  │                        commands, current_label, state)         │
  │  6. state         = apply_transition_decision(state,           │
  │                        label_state, decision, current_label)   │
  │  7. write_label_event(state, prev, current, decision)          │
  └─────────────────────────────────────────────────────────────┘
  ... tracker.apply (L1334) ...
  ... priority.reorder (L1339) ...
  ... executor (L1347) ...
```

Design stance:
- The LTL **reads** the shaped `commands` and **rewrites** them (add/replace/drop `start_flow`/`cancel_flow`/`set_slot`/`clarify`). It never touches `flow_stack` — `tracker.apply` does that from the commands.
- It is **tenant-gated** exactly like the coercions (`request.tenant_id == "salary_on_time"` in Phase 1), so it cannot regress other tenants.
- It is **feature-flagged** (`LABEL_TRANSITION_ENABLED`, default off) so it can be shipped dark and enabled per environment.
- Decisions are pure functions of `(previous_label, current_label, state, label_state)` → easy to unit test and to log.

---

## 4. Data Structures

### 4.1 Storage decision: `slots` first, typed fields later

Phase 1 stores **everything in `ConversationState.slots`** under a single reserved, underscore-prefixed key `_label` (so it is auto-excluded from the LLM prompt — see `command_gen.READ_ONLY_LLM_SLOTS` and the `_`-prefix filter, report §6.4). This means:
- **No schema migration**, no `version`/persistence changes, no Upstash/Postgres format change.
- Backward compatible: old states without `_label` simply start empty (defaults applied by `load_label_state`).

Phase 3+ (optional): promote to a typed `LabelState` sub-model on `ConversationState` once the shape is stable. Because the Phase-1 blob is already a well-defined dict, promotion is a mechanical `model_validate` with a fallback.

### 4.2 The `_label` blob (Phase 1, inside `state.slots["_label"]`)

```python
# conceptual shape (see schemas/label_transition.py in §7)
{
  "active_label": "payment.will_pay_today" | None,
  "previous_label": "refusal.hard_refusal" | None,
  "label_history": [
     {"ts": "...", "label": "refusal.hard_refusal", "turn": 3, "decision": "resolve_previous_and_switch"},
     ...
  ],
  "unresolved_high_risk_labels": [
     {"label": "dispute.loan_not_taken", "since_turn": 2, "evidence": 1, "resolution": None}
  ],
  "evidence_by_label": {
     "dispute.loan_not_taken": 1,
     "refusal.soft_refusal": 2
  },
  "resolved_labels": ["refusal.hard_refusal"]
}
```

| Field | Purpose | Read by | Written by |
|-------|---------|---------|------------|
| `active_label` | current intent label after this turn's decision | matrix, analytics | `apply_transition_decision` |
| `previous_label` | label carried from prior turn | `transition_allowed` | `apply_transition_decision` (rotates active→previous) |
| `label_history` | append-only per-turn record | analytics, audit, tests | `write_label_event` |
| `unresolved_high_risk_labels` | high-risk labels not yet resolved/handed off | `is_high_risk_unresolved`, matrix | `update_evidence`, `resolve_previous_label` |
| `evidence_by_label` | generalized cross-turn counter (superset of today's `_dispute_evidence`) | `transition_allowed`, decay | `update_evidence`, `decay_previous_label` |
| `resolved_labels` | labels explicitly resolved this call | analytics | `resolve_previous_label` |

### 4.3 Label taxonomy (Phase 1 — dotted `namespace.label`)

Kept deliberately small and mapped 1:1 to existing SOT flows / coercion outputs so Phase 1 needs **no new classifier**.

| Namespace | Labels | Inferred from (Phase 1) |
|-----------|--------|--------------------------|
| `identity` | `wrong_person`, `third_party`, `confirmed` | `sot_identity_response`/`sot_knows_customer` slots, `_coerce_sot_identity` |
| `dispute` | `loan_not_taken`, `wrong_amount`, `already_paid`, `fraud` | `_sot_dispute_flow` matcher, `sot_obj_*` start_flow |
| `refusal` | `hard_refusal`, `soft_refusal` | `sot_payment_intent=refused`, reversal coercion, push slots |
| `hardship` | `salary_not_received`, `job_loss`, `medical` | `sot_obj_medical`/hardship flows, transcript cues |
| `payment` | `will_pay_today`, `promise_future_date`, `partial` | `sot_payment_intent=willing`, `sot_customer_time`, `promise_to_pay` |
| `support` | `payment_link_request`, `diff_number_link`, `no_link_pref`, `callback_request` | `sot_obj_link_request`/`sot_obj_diff_number_link` start_flow |
| `risk` | `legal_threat`, `harassment_complaint`, `vulnerable` | safety pre-empt signals, transcript cues |
| `compliance` | `opt_out` | gate opt-out detection |
| `emotion` | `frustration` | emotion slots + frustration guard |

`HIGH_RISK_LABELS = {dispute.*, risk.legal_threat, risk.harassment_complaint, identity.wrong_person, compliance.opt_out}`.

### 4.4 Relationship to existing state

- `evidence_by_label["dispute.*"]` **subsumes** today's `slots["_dispute_evidence"]`; Phase 1 can mirror/replace it behind the flag so both stay consistent.
- `unresolved_high_risk_labels` is new — it is the field the report §14 flagged as missing.
- `label_history` is the general analog of `persona_history`.

---
## 5. Transition Matrix

Decisions: `continue_current_flow`, `switch_flow`, `resolve_previous_and_switch`, `clarify_before_switch`, `block_switch_due_to_high_risk`, `escalate_to_human`, `keep_high_risk_flag_but_allow_payment`, `decay_previous_label`, `accumulate_evidence`.

"Flow command" is expressed in the **existing** command vocabulary that `tracker.apply` understands. `∅` = leave commands unchanged.

| # | previous_label | current_label | decision | action | flow command | risk behavior |
|---|----------------|---------------|----------|--------|--------------|---------------|
| 1 | refusal.hard_refusal | support.payment_link_request | resolve_previous_and_switch | mark refusal resolved, open link flow | `cancel_flow(refusal?)` + `start_flow(sot_obj_link_request)` | none |
| 2 | refusal.soft_refusal | payment.promise_future_date | resolve_previous_and_switch | resolve refusal, capture PTP | `set_slot(sot_payment_intent=willing)` → keep `sot_commit` | none |
| 3 | refusal.soft_refusal | payment.will_pay_today | resolve_previous_and_switch | resolve refusal, proceed to commit | `set_slot(sot_payment_intent=willing)` | none |
| 4 | hardship.salary_not_received | payment.promise_future_date | resolve_previous_and_switch | resolve hardship **for routing**, keep in analytics | `set_slot(sot_payment_intent=willing)`; record hardship in `label_history` | keep hardship label in history only |
| 5 | hardship.job_loss | support.payment_link_request | resolve_previous_and_switch | send link but keep hardship analytics | `start_flow(sot_obj_link_request)` | hardship stays in `label_history` |
| 6 | dispute.loan_not_taken | support.payment_link_request | block_switch_due_to_high_risk | do **not** send link; clarify or transfer | replace with `clarify` or `start_flow(sot_obj_never_loan)` | unresolved dispute blocks payment |
| 7 | dispute.wrong_amount | support.payment_link_request | clarify_before_switch | confirm amount before link | `clarify` | keep dispute unresolved |
| 8 | dispute.already_paid | support.payment_link_request | clarify_before_switch | verify last payment before link | `clarify` (or `sot_obj_already_paid_q`) | keep dispute unresolved |
| 9 | risk.legal_threat | payment.will_pay_today | keep_high_risk_flag_but_allow_payment | allow payment, keep legal flag | `∅` (allow commit) | legal flag stays in `unresolved_high_risk_labels` + audit |
| 10 | risk.legal_threat | support.payment_link_request | keep_high_risk_flag_but_allow_payment | allow link, keep flag | `start_flow(sot_obj_link_request)` | flag retained |
| 11 | risk.harassment_complaint | payment.will_pay_today | keep_high_risk_flag_but_allow_payment | allow payment, keep complaint flag | `∅` | flag retained, audit |
| 12 | identity.wrong_person | support.payment_link_request | block_switch_due_to_high_risk | do not send link pre-verification | drop `start_flow`; force identity | defer to identity gate |
| 13 | identity.wrong_person | payment.will_pay_today | block_switch_due_to_high_risk | verify identity first | force `identity_verification` | no debt disclosure |
| 14 | identity.third_party | support.payment_link_request | clarify_before_switch | confirm authorization | `clarify` | no debt disclosure until authorized |
| 15 | dispute.loan_not_taken | dispute.loan_not_taken | accumulate_evidence | repeated denial → force dispute route | `start_flow(sot_obj_never_loan)` at bar | escalates risk |
| 16 | refusal.hard_refusal | refusal.hard_refusal | accumulate_evidence | repeated hard refusal | after bar → `escalate_to_human` | — |
| 17 | emotion.frustration | * (any) | escalate_to_human (if streak≥N) | hand off | `start_flow(transfer)` / disposition | defers to frustration guard |
| 18 | compliance.opt_out | * (any) | block_switch_due_to_high_risk | honor opt-out, silence | drop commands; gate silences | compliance-critical |
| 19 | payment.promise_future_date | support.payment_link_request | switch_flow | borrower now wants link | `start_flow(sot_obj_link_request)` | none |
| 20 | payment.will_pay_today | support.payment_link_request | switch_flow | send link | `start_flow(sot_obj_link_request)` | none |
| 21 | support.payment_link_request | support.no_link_pref | switch_flow | borrower prefers no link | `start_flow(sot_obj_no_link_pref)` | none |
| 22 | * (same as current) | * (same) | continue_current_flow | stay on rails | `∅` | none |
| 23 | dispute.loan_not_taken | payment.will_pay_today | resolve_previous_and_switch | dispute withdrawn, allow payment | `set_slot(...)`; mark dispute resolved | clears unresolved dispute |
| 24 | hardship.medical | refusal.soft_refusal | continue_current_flow | stay in hardship/push handling | `∅` | keep hardship in history |
| 25 | risk.legal_threat | dispute.loan_not_taken | escalate_to_human | two high-risk signals | `start_flow(transfer)` | both flags retained + audit |
| 26 | None (opener) | any | switch_flow / continue | normal first-turn routing | `∅` | set active_label only |
| 27 | * (any) | * (any, N turns elapsed) | decay_previous_label | age out stale evidence | `∅` | decrement `evidence_by_label`, drop if 0 |
| 28 | refusal.hard_refusal | dispute.wrong_amount | switch_flow (+accumulate) | new dispute appears | `start_flow(sot_obj_wrong_amount)` | add dispute to unresolved |

Notes:
- Rows 6–8, 12–14 are the **safety-critical** additions: they turn today's implicit gap into an explicit block/clarify.
- Rows 9–11 implement `keep_high_risk_flag_but_allow_payment` — payment proceeds but the risk label is **not** discarded.
- Row 27 is the generic `decay_previous_label` mechanism.
- Where a decision overlaps an existing guard (17 frustration, 18 opt-out, 12–13 identity), the LTL **defers** to that guard rather than duplicating it (see §9).

---
## 6. Integration Point in `handle_turn`

Candidate positions and trade-offs:

| Position | Pro | Con |
|----------|-----|-----|
| Before `command_gen` | Could bias the LLM prompt | No `current_label` yet (depends on LLM/coercion output); would need its own inference; risks double routing |
| After `command_gen`, before coercions | Sees raw LLM intent | Coercions may still override → LTL decision becomes stale |
| **After SOT coercions + after confidence floor + after accumulator, before `tracker.apply`** (~L1330) | Sees the **final** proposed route; can still rewrite commands cleanly; `tracker.apply`/`reorder` execute the result | Must read `previous_label` from state before it is rotated |
| After `tracker.apply` | flow_stack already mutated | Would have to *undo* pushes to block a switch — messy, fights the stack |
| After executor | — | Too late; reply already drafted |

**Recommendation: run the LTL at ~L1330, immediately after the dispute accumulator block (L1329) and immediately before `commands_payload`/`tracker.apply` (L1331–1334).**

Rationale:
- At this point `commands` reflect **everything**: LLM + all six SOT coercions + slot validation + confidence-floor suppression + accumulator forcing. `current_label` inference is therefore stable.
- Rewriting `commands` here means `tracker.apply` + `priority.reorder` + executor do all the actual flow-stack work — the LTL reuses them exactly as required (design requirement #3).
- `previous_label` is still intact in `state.slots["_label"]` (rotation happens inside `apply_transition_decision`).

Concretely, the new block is tenant- and flag-gated:

```python
# ~L1330, before commands_payload = [...]
if settings.label_transition_enabled and request.tenant_id == "salary_on_time":
    state, commands, label_decision = run_label_transition(
        state=state,
        commands=commands,
        awaiting_slot=sot_awaiting_slot,
        transcript=request.transcript,
        candidate_flows=candidate_flows,
        dispute_theme=evidence_theme,          # reuse accumulator's signal
        dispute_forced=dispute_forced,
    )
    if label_decision:
        command_rejections = [*command_rejections, f"label_transition:{label_decision.decision}"]
```

The single public entry `run_label_transition(...)` orchestrates the pseudocode functions in §8 and is the only thing `turn.py` imports.

The decision is also surfaced to the existing decision log by extending the `guards` payload in `log_turn_decision` (report §4/§7 note the `guards` field already exists at `turn.py` L1500–1506) — **no signature break**, just an extra key.

---
## 7. New Files and Functions

### 7.1 `Collection/app/schemas/label_transition.py` (new)

Pure data / enums. No logic, no I/O.

| Symbol | Kind | Notes |
|--------|------|-------|
| `Label` | `str` enum-like constants (or `Literal` union) | `identity.wrong_person`, `dispute.loan_not_taken`, … (§4.3) |
| `Decision` | `str` enum | the nine decisions |
| `HIGH_RISK_LABELS` | `frozenset[str]` | §4.3 |
| `LabelEvidence` | pydantic model | `{label, count, since_turn}` |
| `UnresolvedRisk` | pydantic model | `{label, since_turn, evidence, resolution}` |
| `LabelStateModel` | pydantic model | typed mirror of the `_label` blob (§4.2); used by `load/save` helpers |
| `TransitionDecision` | pydantic model | `{decision, current_label, previous_label, reason, forced_flow}` |

Dependencies: `pydantic` only. **No** import from `engine/*` (avoid cycles).

### 7.2 `Collection/app/engine/label_transition.py` (new)

All logic; depends on `schemas/label_transition.py`, `schemas/state.py`, `schemas/command`, `config.py`. Imported by `turn.py` only.

| Function | Input | Output | Purpose |
|----------|-------|--------|---------|
| `run_label_transition(state, commands, awaiting_slot, transcript, candidate_flows, dispute_theme, dispute_forced)` | full turn context | `(state, commands, TransitionDecision \| None)` | orchestrator called by `turn.py` |
| `detect_current_labels(commands, awaiting_slot, transcript, candidate_flows)` | shaped commands + cues | `str \| None` (current_label) | rules-only inference (Phase 1) |
| `load_label_state(state)` | `ConversationState` | `LabelStateModel` | read `_label` blob w/ defaults |
| `save_label_state(state, label_state)` | both | `ConversationState` | write blob back to `slots["_label"]` |
| `update_evidence(label_state, current_label, turn_no)` | — | `LabelStateModel` | bump `evidence_by_label`, maintain `unresolved_high_risk_labels` |
| `transition_allowed(previous_label, current_label, state, label_state)` | labels + state | `TransitionDecision` | consult `TRANSITION_MATRIX` + guards |
| `is_high_risk_unresolved(label_state, current_label)` | — | `bool` | true if a high-risk label is unresolved and current is payment/link |
| `resolve_previous_label(label_state, label)` | — | `LabelStateModel` | move label to `resolved_labels`, drop from unresolved |
| `build_transition_commands(decision, commands, current_label, state)` | — | `list[Command]` | translate decision → command vocabulary |
| `apply_transition_decision(state, label_state, decision, current_label, turn_no)` | — | `(state, LabelStateModel)` | rotate active→previous, set active, persist |
| `write_label_event(state, previous_label, current_label, decision)` | — | `ConversationState` | append to `label_history` + `state.events` (`Event(kind="label_transition")`) |
| `TRANSITION_MATRIX` | module constant | `dict[(prev,cur) → row]` + wildcard fallbacks | §5 encoded as data |
| `LABEL_TO_FLOW` | module constant | `dict[str → flow name]` | maps labels → SOT flows |

### 7.3 `Collection/app/config.py` (extend, no new file)

New settings (env-aliased, defaults keep layer **off** and non-breaking):

| Setting | Env | Default |
|---------|-----|---------|
| `label_transition_enabled` | `LABEL_TRANSITION_ENABLED` | `false` |
| `label_evidence_bar` | `LABEL_EVIDENCE_BAR` | `2` (reuse dispute bar semantics) |
| `label_evidence_decay_turns` | `LABEL_DECAY_TURNS` | `3` |
| `label_high_risk_block` | `LABEL_HIGH_RISK_BLOCK` | `true` |

### 7.4 `Collection/app/engine/turn.py` (extend, ~6 lines)

Single call site at ~L1330 (§6) + one extra key in the `guards` log payload. No signature changes.

---
## 8. Pseudocode

> Phase 1 = rules only. All functions are pure except `run_label_transition`, which threads `state`.

```python
def run_label_transition(state, commands, awaiting_slot, transcript,
                         candidate_flows, dispute_theme, dispute_forced):
    turn_no = state.attempts
    label_state = load_label_state(state)
    previous_label = label_state.active_label   # active from prior turn = "previous" now

    current_label = detect_current_labels(commands, awaiting_slot, transcript, candidate_flows)
    if current_label is None:
        return state, commands, None            # nothing to decide this turn

    label_state = update_evidence(label_state, current_label, turn_no)
    label_state = decay_previous_label(label_state, turn_no)   # row 27

    decision = transition_allowed(previous_label, current_label, state, label_state)

    commands = build_transition_commands(decision, commands, current_label, state)
    state, label_state = apply_transition_decision(state, label_state, decision,
                                                   current_label, turn_no)
    state = write_label_event(state, previous_label, current_label, decision)
    state = save_label_state(state, label_state)
    return state, commands, decision


def detect_current_labels(commands, awaiting_slot, transcript, candidate_flows):
    # 1) strongest signal: an explicit start_flow the earlier layers settled on
    for c in commands:
        if c.command == "start_flow" and c.flow in LABEL_TO_FLOW.inverse:
            return LABEL_TO_FLOW.inverse[c.flow]        # e.g. sot_obj_link_request -> support.payment_link_request
    # 2) coercion/slot signals
    for c in commands:
        if c.command == "set_slot":
            if c.name == "sot_payment_intent" and c.value == "willing":
                return "payment.will_pay_today" if _today_cue(transcript) else "payment.promise_future_date"
            if c.name == "sot_payment_intent" and c.value == "refused":
                return "refusal.hard_refusal"
            if c.name == "sot_identity_response" and c.value == "denied":
                return "identity.wrong_person"
    # 3) deterministic dispute matcher (reuse turn._sot_dispute_flow)
    disp = _sot_dispute_flow(transcript)
    if disp:
        return _flow_to_label(disp)
    # 4) transcript cue lexicons (reuse existing cue tuples where possible)
    if _matches(transcript, LINK_CUES):     return "support.payment_link_request"
    if _matches(transcript, LEGAL_CUES):    return "risk.legal_threat"
    if _matches(transcript, HARDSHIP_CUES): return "hardship.salary_not_received"
    return None                              # unknown -> no transition


def load_label_state(state):
    raw = state.slots.get("_label")
    return LabelStateModel.model_validate(raw) if raw else LabelStateModel()   # defaults


def update_evidence(label_state, current_label, turn_no):
    label_state.evidence_by_label[current_label] = \
        label_state.evidence_by_label.get(current_label, 0) + 1
    if current_label in HIGH_RISK_LABELS:
        if current_label not in [u.label for u in label_state.unresolved_high_risk_labels]:
            label_state.unresolved_high_risk_labels.append(
                UnresolvedRisk(label=current_label, since_turn=turn_no,
                               evidence=1, resolution=None))
        else:
            _bump(label_state.unresolved_high_risk_labels, current_label)
    return label_state


def transition_allowed(previous_label, current_label, state, label_state):
    # 0) defer to existing guards first (do not duplicate)
    if state.slots.get("opt_out"):      return Decision.block_switch_due_to_high_risk
    if _frustration_escalating(state):  return Decision.escalate_to_human

    # 1) high-risk block: unresolved dispute/identity + payment/link intent
    if is_high_risk_unresolved(label_state, current_label):
        row = MATRIX_HIGH_RISK.get((_ns(_worst_unresolved(label_state)), _ns(current_label)))
        return row.decision if row else Decision.clarify_before_switch

    # 2) generic evidence bar (repeated same high-stakes label)
    if current_label in HIGH_RISK_LABELS and \
       label_state.evidence_by_label[current_label] >= settings.label_evidence_bar:
        return Decision.accumulate_evidence   # -> forced route in build_transition_commands

    # 3) matrix lookup with wildcard fallback
    row = (TRANSITION_MATRIX.get((previous_label, current_label))
           or TRANSITION_MATRIX.get(("*", current_label))
           or TRANSITION_MATRIX.get((previous_label, "*")))
    if row is None:
        return Decision.continue_current_flow if previous_label == current_label \
               else Decision.switch_flow
    return row.decision


def is_high_risk_unresolved(label_state, current_label):
    if not settings.label_high_risk_block:
        return False
    wants_money_path = _ns(current_label) in {"support", "payment"}
    has_open_risk = any(u.resolution is None for u in label_state.unresolved_high_risk_labels)
    return wants_money_path and has_open_risk


def resolve_previous_label(label_state, label):
    for u in label_state.unresolved_high_risk_labels:
        if u.label == label:
            u.resolution = "resolved"
    if label and label not in label_state.resolved_labels:
        label_state.resolved_labels.append(label)
    return label_state


def build_transition_commands(decision, commands, current_label, state):
    if decision == Decision.continue_current_flow:
        return commands
    if decision in (Decision.switch_flow, Decision.resolve_previous_and_switch,
                    Decision.accumulate_evidence):
        target = LABEL_TO_FLOW[current_label]
        if not _has_start_flow(commands, target):
            commands = _drop_start_flows(commands) + [Command("start_flow", flow=target)]
        return commands
    if decision == Decision.block_switch_due_to_high_risk:
        # strip payment/link start_flow; either clarify or route to the dispute/identity flow
        commands = _drop_start_flows(commands)
        risk_flow = _flow_for_worst_unresolved(state)     # e.g. sot_obj_never_loan / identity_verification
        return [Command("start_flow", flow=risk_flow)] if risk_flow else [Command("clarify")]
    if decision == Decision.clarify_before_switch:
        return _drop_start_flows(commands) + [Command("clarify")]
    if decision == Decision.escalate_to_human:
        return _drop_start_flows(commands) + [Command("start_flow", flow="sot_obj_talk_to_human")]
    if decision == Decision.keep_high_risk_flag_but_allow_payment:
        return commands                     # allow payment; flag retained in state (below)
    return commands


def apply_transition_decision(state, label_state, decision, current_label, turn_no):
    if decision in (Decision.resolve_previous_and_switch,):
        label_state = resolve_previous_label(label_state, label_state.active_label)
    if decision == Decision.keep_high_risk_flag_but_allow_payment:
        pass                                # explicitly do NOT resolve the risk label
    label_state.previous_label = label_state.active_label
    label_state.active_label = current_label
    return state, label_state


def write_label_event(state, previous_label, current_label, decision):
    label_state = load_label_state(state)
    label_state.label_history.append({
        "ts": now_iso(), "turn": state.attempts,
        "previous": previous_label, "label": current_label,
        "decision": decision,
    })
    state.events.append(Event(kind="label_transition",
                              data={"previous": previous_label, "current": current_label,
                                    "decision": decision}))
    return save_label_state(state, label_state)


def decay_previous_label(label_state, turn_no):          # row 27
    for lbl, cnt in list(label_state.evidence_by_label.items()):
        last = _last_seen_turn(label_state, lbl)
        if turn_no - last >= settings.label_evidence_decay_turns:
            label_state.evidence_by_label[lbl] = max(0, cnt - 1)
            if label_state.evidence_by_label[lbl] == 0:
                label_state.evidence_by_label.pop(lbl, None)
    return label_state
```

---
## 9. Interaction With Existing Routing Features

| Existing feature | Interaction | Ownership rule |
|------------------|-------------|----------------|
| **SOT coercions** (`turn.py` L170–494) | LTL runs **after** them and consumes their output as a `current_label` signal (`detect_current_labels` step 2–3). It never re-implements them. | Coercions win on single-turn slot mapping; LTL only adds cross-turn/transition decisions. |
| **Dispute accumulator** (`turn.py` L621) | LTL **subsumes** it generically via `evidence_by_label`. Phase 1: run LTL after the accumulator and reuse its `evidence_theme`/`dispute_forced` outputs (passed in) to avoid double-counting. Phase 3: accumulator can be retired behind the flag. | Until Phase 3, accumulator remains source of truth for disputes; LTL mirrors. |
| **Frustration guard** (`robustness.py` L107) | Row 17 defers: if the guard is already escalating (`frustration_escalate`), LTL returns `escalate_to_human` but does not duplicate the disposition. | Guard owns escalation mechanics. |
| **Confidence floor** (`turn.py` L557) | LTL runs after it. A label-driven `switch_flow`/`resolve_previous_and_switch` can **re-add** a `start_flow` the floor dropped (like the accumulator does) — but only for high-value labels in the matrix, so it does not reintroduce noisy jumps. | Floor filters KB noise; LTL overrides only for explicit label transitions. |
| **Pinned flows** (`turn.py` L529) | Complementary: pinned flows ensure the candidate/route exists; LTL decides whether the transition to it is allowed. LTL reduces reliance on pinning being enabled. | Independent; both can be on. |
| **`priority.reorder`** (`priority.py` L36) | LTL emits `start_flow`/`cancel_flow`; reorder then parks/activates as usual. For `block_switch_due_to_high_risk` LTL routes to the risk flow (higher priority) so reorder naturally makes it active. | reorder owns stack ordering. |
| **`tracker.apply`** (`tracker.py` L78) | The only executor of LTL's commands. LTL never mutates `flow_stack` directly. | tracker owns stack mutation. |
| **command_gen JSON schema** (`command_gen.py` L486) | Unaffected — LTL runs post-LLM. `LABEL_TO_FLOW` targets must be valid flow names so `tracker.apply` accepts them (same constraint as coercions). | Schema unchanged. |
| **Active collect slot** (`_awaiting_collect_slot` L151) | Passed into `detect_current_labels`; a mid-collect turn biases labels (e.g. awaiting `sot_link_received` → link namespace) and lets LTL choose `continue_current_flow`. | LTL respects on-rails context. |
| **Compliance gate** (`gate.py` L18) | Runs later on the outbound reply, independently. Opt-out (row 18) defers to it. LTL never overrides the gate. | Gate owns outbound compliance. |
| **Human transfer** (`actions.py` L808) | `escalate_to_human`/high-risk routes point to a transfer flow; the transfer action is unchanged. | Actions own transfer side effects. |
| **WhatsApp / payment link action** (`actions.py` L774) | LTL can **prevent** reaching the link flow (block/clarify) but does not touch the action itself. This is the direct fix for Trace B. | Action unchanged; gating happens upstream. |
| **Terminal guard** (`turn.py` L1113) | Runs before LTL; if the call is already closed, LTL never runs (early exit). | Guard owns terminal state. |
| **Identity entry gate** (`identity_gate.py` L110) | Rows 12–13 defer to it: LTL drops the premature link `start_flow`; the identity gate (which runs in the reorder block) inserts `identity_verification`. | Identity gate owns verification ordering. |

Guiding principle: **LTL is a decision layer, not an execution layer.** Every side effect is delegated to an existing mechanism.

---
## 10. Test Plan

### 10.1 `Collection/tests/unit/test_label_transition.py` (new — pure logic, fast)

Test the layer functions directly with hand-built `LabelStateModel` + command lists (no LLM/KB):

| Test | Asserts |
|------|---------|
| `test_detect_current_label_from_start_flow` | `sot_obj_link_request` → `support.payment_link_request` |
| `test_detect_current_label_from_slot_and_cue` | willing + "aaj" → `payment.will_pay_today`; willing + "4 din baad" → `payment.promise_future_date` |
| `test_matrix_refusal_then_link_resolves_and_switches` | rows 1: decision `resolve_previous_and_switch`, commands contain `start_flow(sot_obj_link_request)` |
| `test_matrix_soft_refusal_then_ptp_resolves_refusal` | row 2: refusal moved to `resolved_labels`; PTP kept |
| `test_matrix_hardship_then_ptp_keeps_analytics` | row 4: `sot_payment_intent=willing`; hardship present in `label_history`, not blocking |
| `test_matrix_loan_not_taken_then_link_blocks` | row 6: no `start_flow(sot_obj_link_request)`; decision `block_switch_due_to_high_risk` |
| `test_matrix_wrong_person_then_link_blocks` | row 12: link start_flow dropped; identity path forced |
| `test_matrix_legal_threat_then_payment_keeps_flag` | row 9: payment allowed; `unresolved_high_risk_labels` still contains `risk.legal_threat` |
| `test_is_high_risk_unresolved_true_and_false` | boolean logic for dispute/identity unresolved + money intent |
| `test_evidence_accumulates_then_forces_route` | row 15: at bar → `accumulate_evidence` forces `start_flow(sot_obj_never_loan)` |
| `test_label_evidence_decay` | after `LABEL_DECAY_TURNS` idle turns, `evidence_by_label` decrements/removes |
| `test_label_history_written` | `label_history` and `Event(kind="label_transition")` appended each transition |
| `test_resolve_previous_label` | label moves to `resolved_labels`, unresolved entry marked resolved |
| `test_layer_noop_when_flag_off` | `LABEL_TRANSITION_ENABLED=false` → commands untouched, no `_label` written |
| `test_layer_defers_to_frustration_and_optout` | rows 17/18 return escalate/block without duplicating disposition |

### 10.2 `Collection/tests/golden/test_label_transition_e2e.py` (new — full `handle_turn`)

Drive real `handle_turn` with `ScriptedLLM`/`ScriptedKB` (pattern from `test_sot_pre_closure.py`), flag on:

| Test (required by prompt) | Scenario | Asserts |
|---------------------------|----------|---------|
| `test_refusal_then_payment_link_switches_flow` | refuse, then "link bhej do" | ends in `sot_obj_link_request`; refusal resolved |
| `test_soft_refusal_then_ptp_resolves_refusal` | soft refuse, then future date | commit flow captures PTP; refusal resolved |
| `test_hardship_then_ptp_keeps_analytics_but_switches` | hardship, then future date | PTP captured; hardship in `label_history` |
| `test_loan_not_taken_then_link_request_clarifies` | "maine loan liya hi nahi", then "link bhej do" | link **not** sent; clarify or transfer |
| `test_wrong_person_then_link_request_does_not_send_link` | deny identity, then "link bhej do" | link **not** sent before verification |
| `test_legal_threat_then_payment_keeps_risk_flag` | legal threat, then "aaj kar dunga" | payment proceeds; legal flag persists in state/audit |
| `test_missed_dispute_then_later_link_is_blocked_if_evidence_unresolved` | soft/paraphrased dispute (matcher misses) accumulates, then link | link blocked because `unresolved_high_risk_labels` non-empty |
| `test_label_evidence_decay` | dispute cue then N neutral turns then link | after decay, link allowed (evidence aged out) — guards the false-positive risk |
| `test_label_history_written` | any multi-turn call | `label_history` reconstructable; `label_transition` events present |

### 10.3 Regression guard

Run existing suites with the flag **off** to prove zero behavior change:
`tests/golden/test_sot_pre_closure.py`, `test_repair_layer.py`, `tests/compliance/test_release_gate.py`. Then a second CI lane with the flag **on** for the new e2e tests.

---
## 11. Implementation Phases

**Phase 1 — Rules-only LTL (no ML).** *Smallest safe slice.*
- Add `schemas/label_transition.py` + `engine/label_transition.py` + 4 config flags.
- `detect_current_labels` uses only flow names, coercion/slot outputs, `_sot_dispute_flow`, and existing cue lexicons.
- Store `_label` blob in `slots` (no schema migration).
- Wire single call at `turn.py` ~L1330 behind `LABEL_TRANSITION_ENABLED` (default off).
- Encode matrix rows §5; defer to frustration/opt-out/identity guards.
- Ship dark, enable in test env, validate against the last-call scenarios (esp. Trace B block).
- Exit criteria: all §10.1 unit tests + §10.2 e2e green; existing suites unchanged with flag off.

**Phase 2 — Model-produced current labels.**
- Add an optional `current_label` field to the LLM command_gen output (schema addition) OR a lightweight classifier call; `detect_current_labels` prefers the model label, falls back to rules.
- Keeps the same matrix + state; only the label source improves (recall on paraphrased intents — the missed-dispute gap).

**Phase 3 — General evidence accumulator + decay.**
- Promote `_label` blob to a typed `LabelState` field on `ConversationState` (with `model_validate` fallback for old blobs).
- Retire the dispute-specific `_dispute_evidence`/`_accumulate_dispute_evidence` in favor of `evidence_by_label` (behind flag); wire decay (`decay_previous_label`).
- Add conversion analytics events (`refusal_then_paid`) derived from `label_history`.

**Phase 4 — Trained classifier / reranker.**
- Train an intent+risk classifier on human-human recordings + labeled call logs (see `MODEL_TRAINING_AND_ARCHITECTURE.md`).
- Serve as a worker (mirrors `semantic_turn`); output feeds `detect_current_labels` and optionally reranks KB candidates.
- Matrix and state layer remain the stable interface; only label production is swapped.

Each phase is independently shippable and reversible via the flag.

---

## 12. Open Questions

1. **Resolution semantics for high-risk labels:** does "resolved" require an explicit borrower withdrawal (row 23) or agent/transfer handling? Proposal: only `resolve_previous_and_switch` on clear withdrawal cues; otherwise keep unresolved and clarify/transfer.
2. **Decay for high-risk labels:** should `dispute.*`/`identity.wrong_person` decay at all, or persist for the whole call? Recommendation: **no decay for high-risk** (only for soft labels like refusal/hardship) to avoid re-opening a blocked link.
3. **Digression-off behavior:** when `SOT_DIGRESSION=false`, should the LTL be allowed to re-add a `start_flow` that on-rails suppression would otherwise strip? Likely yes for a small allow-list of high-value labels (link/PTP) — needs product sign-off.
4. **Relationship to `enforce_compliance_gate=false` for SOT:** LTL blocks are routing-level, not compliance-level. Confirm this is acceptable given SOT disables the compliance gate (report §5 #24).
5. **`today` vs `future` disambiguation** for `payment.will_pay_today` vs `payment.promise_future_date` currently relies on transcript cues + `resolve_today`; is that reliable enough, or should it read the `sot_customer_time` slot only?
6. **Where should conversion analytics live** (Phase 3) — on `BorrowerRecord` (cross-call) or in audit only? Affects retention/PII.
7. **Label taxonomy governance:** who owns adding new labels + matrix rows, and how are they validated (a manifest/lint like `reply_manifest.json`)?
8. **Double-source of dispute evidence** during Phase 1/2 (accumulator + LTL): confirm the mirroring strategy avoids drift before retiring the accumulator in Phase 3.

---

*End of design report. No application code was modified. Next step (on approval): implement Phase 1 behind `LABEL_TRANSITION_ENABLED`, starting with `schemas/label_transition.py` and the §10.1 unit tests.*









