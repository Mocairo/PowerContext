import pytest
from pydantic import ValidationError

from labrelay.domain.models import ALLOWED_TASK_TRANSITIONS, Citation, LearningTask

CITATION_DICT = {
    "asset_id": "asset-1",
    "kind": "code",
    "title": "train.py",
    "uri": "repo/train.py",
    "locator": {"line_start": 1, "line_end": 20},
    "excerpt": "def train(...)",
}


def task_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "task-1",
        "project_id": "project-1",
        "plan_id": "plan-1",
        "stage": 0,
        "title": "了解实验室资源规则",
        "objective": "知道如何申请和释放 GPU",
        "estimated_minutes": 20,
        "completion_criteria": "能复述规则",
    }
    payload.update(overrides)
    return payload


def test_citation_requires_asset_id_and_valid_kind() -> None:
    citation = Citation(
        asset_id="asset-1",
        kind="code",
        title="train.py",
        uri="repo/train.py",
        locator={"line_start": 1, "line_end": 20},
        excerpt="def train(...)",
    )
    assert citation.version is None

    with pytest.raises(ValidationError, match="asset_id"):
        Citation.model_validate({"kind": "code", "title": "train.py", "uri": "repo/train.py"})

    with pytest.raises(ValidationError, match="kind"):
        Citation.model_validate({**CITATION_DICT, "kind": "slide-deck"})


def test_task_transition_rules_do_not_allow_done_to_todo() -> None:
    assert "blocked" in ALLOWED_TASK_TRANSITIONS["in_progress"]
    assert "todo" not in ALLOWED_TASK_TRANSITIONS["done"]


def test_skipped_is_a_terminal_state_with_no_outgoing_transitions() -> None:
    assert ALLOWED_TASK_TRANSITIONS["skipped"] == set()
    assert ALLOWED_TASK_TRANSITIONS["done"] == set()
    # reachable, but never escapable
    assert "skipped" in ALLOWED_TASK_TRANSITIONS["todo"]
    assert "skipped" in ALLOWED_TASK_TRANSITIONS["in_progress"]
    assert "skipped" in ALLOWED_TASK_TRANSITIONS["blocked"]
    assert "todo" not in ALLOWED_TASK_TRANSITIONS["skipped"]


def test_task_requires_citation_when_configured() -> None:
    with pytest.raises(ValueError, match="Learning task requires a citation"):
        LearningTask.model_validate({**task_payload(), "citations": []})


def test_citation_required_task_rejects_omitted_citations() -> None:
    with pytest.raises(ValueError, match="Learning task requires a citation"):
        LearningTask.model_validate(task_payload(task_type="citation_required"))

    with pytest.raises(ValueError, match="Learning task requires a citation"):
        LearningTask.model_validate(task_payload())


def test_citation_required_task_validates_with_an_explicit_citation() -> None:
    task = LearningTask.model_validate({**task_payload(), "citations": [CITATION_DICT]})
    assert task.task_type == "citation_required"
    assert [c.asset_id for c in task.citations] == ["asset-1"]
    assert LearningTask.model_validate(task.model_dump()) == task


def test_self_assessment_task_needs_no_citation_and_survives_round_trip() -> None:
    task = LearningTask.model_validate(task_payload(task_type="self_assessment"))
    assert task.citations == []
    assert task.model_dump()["citations"] == []

    assert LearningTask.model_validate(task.model_dump()) == task

    explicit_empty = LearningTask.model_validate(
        task_payload(task_type="self_assessment", citations=[])
    )
    assert explicit_empty.citations == []
