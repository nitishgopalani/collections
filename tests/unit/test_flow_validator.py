import pytest

from app.flows.loader import load_flow_yaml
from app.flows.validator import validate_flow
from app.schemas.flow import Flow


def test_invalid_branch_target_fails_load(tmp_path):
    path = tmp_path / "bad.yml"
    path.write_text(
        """
flows:
  bad_flow:
    description: broken
    priority: reminder
    steps:
      - id: only
        next: missing_step
responses: {}
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="does not exist"):
        load_flow_yaml(path)


def test_progress_free_loop_fails_validation():
    flow = Flow.model_validate(
        {
            "description": "loop",
            "priority": "reminder",
            "steps": [
                {
                    "id": "loop",
                    "decide": [{"if": "flag == true", "then": "loop"}],
                },
            ],
        }
    )
    with pytest.raises(ValueError, match="progress-free loop"):
        validate_flow("loop_flow", flow)
