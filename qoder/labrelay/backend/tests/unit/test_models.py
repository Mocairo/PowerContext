import pytest

from labrelay.domain.models import ALLOWED_TASK_TRANSITIONS, Citation, LearningTask


def test_citation_requires_asset_id_and_kind() -> None:
    citation = Citation(
        asset_id="asset-1",
        kind="code",
        title="train.py",
        uri="repo/train.py",
        locator={"line_start": 1, "line_end": 20},
        excerpt="def train(...)",
    )
    assert citation.version is None


def test_task_transition_rules_do_not_allow_done_to_todo() -> None:
    assert "blocked" in ALLOWED_TASK_TRANSITIONS["in_progress"]
    assert "todo" not in ALLOWED_TASK_TRANSITIONS["done"]


def test_task_requires_citation_when_configured() -> None:
    with pytest.raises(ValueError, match="Learning task requires a citation"):
        LearningTask.model_validate(
            {
                "id": "task-1",
                "project_id": "project-1",
                "plan_id": "plan-1",
                "stage": 0,
                "title": "了解实验室资源规则",
                "objective": "知道如何申请和释放 GPU",
                "estimated_minutes": 20,
                "completion_criteria": "能复述规则",
                "citations": [],
            }
        )
