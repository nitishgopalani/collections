from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.schemas.flow import FlowBranch, FlowSet, FlowStep
from app.schemas.state import ConversationState

END_TARGET = "end"
MAX_EXECUTOR_STEPS = 256

ActionRunner = Callable[[str, ConversationState], ConversationState]
ActionRunnerAsync = Callable[[str, ConversationState], Awaitable[ConversationState]]


@dataclass
class ExecResult:
    state: ConversationState
    reply_id: str | None = None
    question_slot: str | None = None
    actions_called: list[str] = field(default_factory=list)
    transfer_to_human: bool = False
    end_call: bool = False
    disposition: str | None = None


def _find_step_index(steps: list[FlowStep], target: str) -> int:
    for index, step in enumerate(steps):
        if step.id == target:
            return index
    raise KeyError(f"Step id '{target}' not found in flow")


def _resolve_branches(branches: list[FlowBranch], slots: dict[str, Any]) -> str:
    fallback: str | None = None
    for branch in branches:
        if branch.else_:
            fallback = branch.else_
            continue
        if branch.if_ is not None and branch.then is not None:
            from app.engine.conditions import evaluate_condition

            if evaluate_condition(branch.if_, slots):
                return branch.then
    if fallback is not None:
        return fallback
    raise ValueError("No branch condition matched and no else branch provided")


def _resolve_next(next_value: str | list[FlowBranch] | None, slots: dict[str, Any]) -> str:
    if next_value is None:
        return END_TARGET
    if isinstance(next_value, str):
        return next_value
    return _resolve_branches(next_value, slots)


def _goto_target(working: ConversationState, steps: list[FlowStep], target: str) -> None:
    if target == END_TARGET:
        if working.slots.pop("_skip_flow_pop", None):
            return
        working.flow_stack.pop()
        # A lone remaining frame must not stay parked (parent was parked under the
        # digression). Without this, resume after objection end is a no-op turn.
        if len(working.flow_stack) == 1:
            working.flow_stack[0].parked = False
    else:
        working.flow_stack[-1].step_index = _find_step_index(steps, target)


def _should_escalate_utter(
    working: ConversationState,
    flows: FlowSet,
    step: FlowStep,
) -> bool:
    """True when attempt-tagged utter has exhausted its highest attempt + escalate_to."""
    if not step.utter or not step.escalate_to:
        return False
    from app.engine.nlg import REPLY_COUNTS_KEY, max_attempt_for_reply

    max_attempt = max_attempt_for_reply(flows, step.utter)
    if max_attempt is None:
        return False
    counts = working.slots.get(REPLY_COUNTS_KEY) or {}
    try:
        prior = int(counts.get(step.utter, 0)) if isinstance(counts, dict) else 0
    except (TypeError, ValueError):
        prior = 0
    return prior >= max_attempt


def run(
    state: ConversationState,
    flows: FlowSet,
    action_runner: ActionRunner,
) -> ExecResult:
    """Walk the active flow stack until pause, reply, or completion."""
    working = state.model_copy(deep=True)
    actions_called: list[str] = []
    reply_id: str | None = None
    question_slot: str | None = None
    steps_taken = 0

    while working.flow_stack:
        steps_taken += 1
        if steps_taken > MAX_EXECUTOR_STEPS:
            frame = working.flow_stack[-1]
            raise RuntimeError(
                f"Executor exceeded {MAX_EXECUTOR_STEPS} steps; possible infinite loop "
                f"in flow '{frame.flow}' at step {frame.step_index}"
            )

        frame = working.flow_stack[-1]
        if frame.parked:
            break

        flow = flows.flows.get(frame.flow)
        if flow is None:
            working.flow_stack.pop()
            continue

        steps = flow.steps
        if frame.step_index >= len(steps):
            working.flow_stack.pop()
            continue

        step = steps[frame.step_index]

        if step.collect:
            slot_name = step.collect
            if slot_name not in working.slots or working.slots[slot_name] is None:
                question_slot = slot_name
                return ExecResult(
                    state=working,
                    reply_id=reply_id,
                    question_slot=question_slot,
                    actions_called=actions_called,
                    transfer_to_human=bool(working.slots.get("transfer_to_human")),
                    end_call=bool(working.slots.get("end_call")),
                )
            # Slot already filled: follow the step's explicit `next` (so e.g.
            # ask_timing -> classify_timing -> route_timing re-runs and routes the
            # captured value). Only fall back to list order when no `next` is set.
            if step.next is not None:
                target = _resolve_next(step.next, working.slots)
                _goto_target(working, steps, target)
            else:
                working.flow_stack[-1].step_index += 1
            continue

        if step.action:
            working = action_runner(step.action, working)
            actions_called.append(step.action)
            # Terminal actions (hangup_call / transfer_call) mark the call closed. Stop
            # the walk here so a parent frame (e.g. an objection pushed on sot_commit)
            # cannot resume and ask another question after we've ended / handed off.
            if working.slots.get("sot_call_closed"):
                break
            target = _resolve_next(step.next, working.slots)
            _goto_target(working, steps, target)
            continue

        if step.decide:
            branches = step.decide or []
            target = _resolve_branches(branches, working.slots)
            _goto_target(working, steps, target)
            continue

        if step.utter:
            if _should_escalate_utter(working, flows, step):
                _goto_target(working, steps, str(step.escalate_to))
                continue
            reply_id = step.utter
            target = _resolve_next(step.next, working.slots)
            _goto_target(working, steps, target)
            continue

        if step.end:
            working.flow_stack.pop()
            # Don't resume a parent ladder after a terminal end_call was set
            # (e.g. identity_refusal → route_identity_failure → close utter).
            if working.slots.get("end_call") or working.slots.get("sot_call_closed"):
                break
            continue

        if isinstance(step.next, str):
            _goto_target(working, steps, step.next)
            continue

        working.flow_stack[-1].step_index += 1

    return ExecResult(
        state=working,
        reply_id=reply_id,
        question_slot=question_slot,
        actions_called=actions_called,
        transfer_to_human=bool(working.slots.get("transfer_to_human")),
        end_call=bool(working.slots.get("end_call")),
    )


async def run_async(
    state: ConversationState,
    flows: FlowSet,
    action_runner: ActionRunnerAsync,
) -> ExecResult:
    """Async executor walk — for handle_turn without nested asyncio.run."""
    working = state.model_copy(deep=True)
    actions_called: list[str] = []
    reply_id: str | None = None
    question_slot: str | None = None
    steps_taken = 0

    while working.flow_stack:
        steps_taken += 1
        if steps_taken > MAX_EXECUTOR_STEPS:
            frame = working.flow_stack[-1]
            raise RuntimeError(
                f"Executor exceeded {MAX_EXECUTOR_STEPS} steps; possible infinite loop "
                f"in flow '{frame.flow}' at step {frame.step_index}"
            )

        frame = working.flow_stack[-1]
        if frame.parked:
            break

        flow = flows.flows.get(frame.flow)
        if flow is None:
            working.flow_stack.pop()
            continue

        steps = flow.steps
        if frame.step_index >= len(steps):
            working.flow_stack.pop()
            continue

        step = steps[frame.step_index]

        if step.collect:
            slot_name = step.collect
            if slot_name not in working.slots or working.slots[slot_name] is None:
                question_slot = slot_name
                return ExecResult(
                    state=working,
                    reply_id=reply_id,
                    question_slot=question_slot,
                    actions_called=actions_called,
                    transfer_to_human=bool(working.slots.get("transfer_to_human")),
                    end_call=bool(working.slots.get("end_call")),
                )
            # Slot already filled: follow the step's explicit `next` (so e.g.
            # ask_timing -> classify_timing -> route_timing re-runs and routes the
            # captured value). Only fall back to list order when no `next` is set.
            if step.next is not None:
                target = _resolve_next(step.next, working.slots)
                _goto_target(working, steps, target)
            else:
                working.flow_stack[-1].step_index += 1
            continue

        if step.action:
            working = await action_runner(step.action, working)
            actions_called.append(step.action)
            # Terminal actions (hangup_call / transfer_call) mark the call closed. Stop
            # the walk here so a parent frame (e.g. an objection pushed on sot_commit)
            # cannot resume and ask another question after we've ended / handed off.
            if working.slots.get("sot_call_closed"):
                break
            target = _resolve_next(step.next, working.slots)
            _goto_target(working, steps, target)
            continue

        if step.decide:
            branches = step.decide or []
            target = _resolve_branches(branches, working.slots)
            _goto_target(working, steps, target)
            continue

        if step.utter:
            if _should_escalate_utter(working, flows, step):
                _goto_target(working, steps, str(step.escalate_to))
                continue
            reply_id = step.utter
            target = _resolve_next(step.next, working.slots)
            _goto_target(working, steps, target)
            continue

        if step.end:
            working.flow_stack.pop()
            # Don't resume a parent ladder after a terminal end_call was set
            # (e.g. identity_refusal → route_identity_failure → close utter).
            if working.slots.get("end_call") or working.slots.get("sot_call_closed"):
                break
            continue

        if isinstance(step.next, str):
            _goto_target(working, steps, step.next)
            continue

        working.flow_stack[-1].step_index += 1

    return ExecResult(
        state=working,
        reply_id=reply_id,
        question_slot=question_slot,
        actions_called=actions_called,
        transfer_to_human=bool(working.slots.get("transfer_to_human")),
        end_call=bool(working.slots.get("end_call")),
    )
