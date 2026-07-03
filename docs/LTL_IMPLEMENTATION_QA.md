# LTL Implementation QA (Phase 1 — Shadow/Enforce)

Status: **in progress**. Feature ships behind `LABEL_TRANSITION_ENABLED` (default **false**).

## Phase 0 — Confirmed code contracts

| Item | Confirmed value | Source |
|------|-----------------|--------|
| Insertion point | After dispute accumulator, before `commands_payload`/`apply(...)` | `app/engine/turn.py` ~L1330–1334 |
| Command schema | `Command(command, flow, name, value, reason)`; commands: `start_flow/set_slot/cancel_flow/clarify/human_handoff/cannot_handle` | `app/schemas/command.py` L5–20 |
| Event schema | `Event(ts: str, kind: str, data: dict, rationale: str\|None)` — **ts required** | `app/schemas/state.py` L12–16 |
| State store | `ConversationState.slots: dict[str,Any]` — write `_label` here (underscore key auto-hidden from LLM) | `app/schemas/state.py` L19–27; `command_gen` READ_ONLY + `_` filter |
| Flow existence check | `flows.flows: dict[str, Flow]` — membership test | `app/schemas/flow.py` L61–63 |
| Settings access | `settings = get_settings()` already in `handle_turn` | `app/engine/turn.py` L1046 |
| Decision log hook | `log_turn_decision(..., guards={...})` — add `label_transition` key | `app/engine/turn.py` L1494–1516 |

### Verified real Salary On Time flow names (from `app/flows/salary_on_time/pre_closure.yml`)

Mapped label → flow (only these are used for enforce):

| Label | Real flow | Line |
|-------|-----------|------|
| `support.payment_link_request` | `sot_obj_link_request` | 671 |
| `support.diff_number_link` | `sot_obj_diff_number_link` | 789 |
| `support.no_link_pref` | `sot_obj_no_link_pref` | 758 |
| `dispute.loan_not_taken` | `sot_obj_never_loan` | 519 |
| `dispute.wrong_amount` | `sot_obj_wrong_amount` | 559 |
| `dispute.already_paid` | `sot_obj_already_paid_q` | 796 |
| `hardship.medical` | `sot_obj_medical` | 529 |
| `hardship.job_loss` | `sot_obj_job_loss` | 539 |

### Conceptual labels with NO real SOT flow — must NOT be invented

`dispute.fraud`, `identity.wrong_person`, `identity.third_party`, `risk.legal_threat`,
`risk.harassment_complaint`, `risk.self_harm_signal`, `compliance.opt_out`,
`payment.*`, `refusal.*`, `hardship.salary_not_received`, `support.callback_request`,
`emotion.frustration`.

For these, enforce degrades to `clarify`/`noop` and defers to existing safety/compliance/identity/transfer mechanisms. (`sot_obj_talk_to_human`, a transfer flow, a generic `identity_verification` SOT flow, fraud/legal/harassment/opt-out/self-harm flows do **not** exist and are not used.)

### Provider strategy
- `salary_on_time` provider: supports **shadow + enforce**, has the label↔flow maps above.
- `generic` provider: **shadow only**, no flow maps (detection/logging without routing changes).

### Circular-import note
`label_transition.py` does **not** import `turn.py` (would cycle). The minimal dispute/identity/link cue logic is duplicated locally in `label_transition.py`.

### Test strategy
- Pure-logic unit tests (`tests/unit/test_label_transition.py`).
- End-to-end `handle_turn` golden tests with flag on (`tests/golden/test_label_transition_e2e.py`).
- Regression: existing SOT/repair/compliance suites with flag off (default).

<!-- QA_BELOW -->

## Phases 1–6 — Delivered

| Phase | Artifact | Status |
|-------|----------|--------|
| 1 | `app/schemas/label_transition.py` (data-only: Decision, Label, models, `HIGH_RISK_LABELS`, `MONEY_PATH_LABELS`) | done |
| 2 | `app/engine/label_transition.py` (providers, detection, decision, enforce command builder, orchestrator) | done |
| 3 | `app/config.py` flags: `LABEL_TRANSITION_ENABLED/MODE/SCOPE`, `LABEL_HIGH_RISK_BLOCK`, `LABEL_TRANSITION_LOG` | done |
| 4 | Wired into `handle_turn` after accumulator / before `commands_payload`+`apply`; guarded by try/except; added to `log_turn_decision` guards + `guards["label_transition"]` | done |
| 5 | `tests/unit/test_label_transition.py` | 24 passed |
| 6 | `tests/golden/test_label_transition_e2e.py` | 4 passed |

## Phase 7 — Regression QA (flag OFF = default)

Command (venv, `PYTHONUTF8=1`):
`pytest tests/golden/test_sot_pre_closure.py tests/golden/test_repair_layer.py tests/unit/test_turn_decision_log.py tests/unit/test_salary_on_time_config.py tests/unit/test_sot_flow_pack.py`

Result: **84 passed** — no behavior change with the layer disabled.

LTL suites: unit **24 passed**, e2e **4 passed**.

## Behavior contracts verified by tests

- **Flag off (default):** no `_label` written, behavior byte-identical (regression suite).
- **Shadow:** labels + `label_transition` audit event recorded; commands never rewritten;
  a link request the LLM missed is **not** routed (`test_shadow_does_not_route_link_when_llm_silent`).
- **Enforce (SOT):** the missed link request **is** force-routed to `sot_obj_link_request`
  with WhatsApp fired (`test_enforce_routes_link_when_llm_silent`) — this is the live-call fix.
- **High-risk safety:** unresolved dispute + money path → `clarify_before_switch` (drops the
  link `start_flow`, adds `clarify`, records `blocked_by`); ownership confirmation →
  `resolve_previous_and_switch`; wrong-person → `block`; legal/harassment →
  `keep_high_risk_flag_but_allow_payment`.
- **Provider gating:** generic tenants are shadow-only; enforce requested on an unsupported
  provider degrades to shadow with `enforcement_skipped_reason="unsupported_provider"`;
  `scope=supported` skips generic tenants entirely.
- **Fail-safe:** LTL is wrapped in try/except in `handle_turn` — any error logs and the turn
  proceeds unchanged.

## Rollout recommendation

1. Ship with `LABEL_TRANSITION_ENABLED=false` (current default) — inert.
2. Turn on **shadow** in prod (`ENABLED=true`, `MODE=shadow`, `SCOPE=supported`); read
   `guards.label_transition` in turn-decision logs to validate detection/decisions on real calls.
3. After shadow looks correct, flip `MODE=enforce` for salary_on_time only.

