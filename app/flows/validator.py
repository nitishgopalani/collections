from app.schemas.flow import Flow, FlowStep

END_TARGET = "end"


def _step_is_progress(step: FlowStep) -> bool:
    return bool(step.collect or step.action or step.utter or step.end)


def _branch_targets(step: FlowStep) -> list[str]:
    targets: list[str] = []
    branches = step.next if isinstance(step.next, list) else []
    if isinstance(step.next, str):
        targets.append(step.next)
    for branch in branches:
        if branch.then:
            targets.append(branch.then)
        if branch.else_:
            targets.append(branch.else_)
    if step.decide:
        for branch in step.decide:
            if branch.then:
                targets.append(branch.then)
            if branch.else_:
                targets.append(branch.else_)
    if step.escalate_to:
        targets.append(step.escalate_to)
    return targets


def _resolve_step_index(steps: list[FlowStep], target: str) -> int | None:
    if target == END_TARGET:
        return None
    for index, step in enumerate(steps):
        if step.id == target:
            return index
    return None


def _step_transitions(flow_name: str, steps: list[FlowStep]) -> dict[int, list[int]]:
    transitions: dict[int, list[int]] = {index: [] for index in range(len(steps))}

    for index, step in enumerate(steps):
        targets = _branch_targets(step)
        if targets:
            for target in targets:
                if target == END_TARGET:
                    continue
                target_index = _resolve_step_index(steps, target)
                if target_index is not None:
                    transitions[index].append(target_index)
        elif step.end:
            continue
        elif index + 1 < len(steps):
            transitions[index].append(index + 1)

    return transitions


def _has_progress_free_cycle(steps: list[FlowStep], transitions: dict[int, list[int]]) -> bool:
    progress_nodes = {index for index, step in enumerate(steps) if _step_is_progress(step)}

    visited: set[int] = set()
    stack: set[int] = set()

    def dfs(node: int, path: list[int]) -> bool:
        visited.add(node)
        stack.add(node)
        path.append(node)

        for neighbor in transitions.get(node, []):
            if neighbor not in visited:
                if dfs(neighbor, path):
                    return True
            elif neighbor in stack:
                cycle_start = path.index(neighbor)
                cycle = path[cycle_start:]
                if all(index not in progress_nodes for index in cycle):
                    return True

        path.pop()
        stack.remove(node)
        return False

    for node in range(len(steps)):
        if node not in visited:
            if dfs(node, []):
                return True
    return False


def validate_flow(flow_name: str, flow: Flow) -> None:
    steps = flow.steps
    known_ids: set[str] = {END_TARGET}
    for step in steps:
        if step.id:
            known_ids.add(step.id)

    for index, step in enumerate(steps):
        for target in _branch_targets(step):
            if target != END_TARGET and target not in known_ids:
                raise ValueError(
                    f"Flow '{flow_name}' step {index}: branch target '{target}' does not exist"
                )
        if isinstance(step.next, str) and step.next not in known_ids:
            raise ValueError(
                f"Flow '{flow_name}' step {index}: next target '{step.next}' does not exist"
            )
        if step.escalate_to and step.escalate_to not in known_ids:
            raise ValueError(
                f"Flow '{flow_name}' step {index}: escalate_to '{step.escalate_to}' "
                "does not exist"
            )

    transitions = _step_transitions(flow_name, steps)
    if _has_progress_free_cycle(steps, transitions):
        raise ValueError(f"Flow '{flow_name}' contains a progress-free loop")


def validate_flow_set(flows: dict[str, Flow]) -> None:
    for name, flow in flows.items():
        validate_flow(name, flow)
