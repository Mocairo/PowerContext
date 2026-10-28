import os
from uuid import uuid4

import pytest

from labrelay.config import LabRelayConfig
from labrelay.domain.models import (
    Asset,
    Citation,
    LearningPlan,
    LearningTask,
    Project,
    TaskEvent,
)
from labrelay.persistence.seekdb import DuplicateAssetLocationError, SeekdbRepository

pytestmark = pytest.mark.skipif(
    not os.getenv("LABRELAY_SEEKDB_TEST"), reason="set LABRELAY_SEEKDB_TEST=1 with a live seekdb"
)


def _config() -> LabRelayConfig:
    return LabRelayConfig(
        seekdb_host=os.getenv("SEEKDB_HOST", "127.0.0.1"),
        seekdb_port=int(os.getenv("SEEKDB_PORT", "2881")),
        seekdb_database=os.getenv("LABRELAY_SEEKDB_TEST_DATABASE", "labrelay_test"),
        seekdb_user=os.getenv("SEEKDB_USER", "root"),
        seekdb_password=os.getenv("SEEKDB_ROOT_PASSWORD", ""),
        seekdb_tenant=os.getenv("SEEKDB_TENANT", "sys"),
    )


async def test_repository_round_trip() -> None:
    repository = SeekdbRepository.from_config(_config())
    await repository.initialize()

    suffix = uuid4().hex[:8]
    project_id = f"project-{suffix}"
    project = Project(
        id=project_id,
        title="Aurora baseline handoff",
        research_area="Deep learning",
        description="A synthetic project used to verify seekdb persistence.",
        project_scope_id=f"project-scope-{suffix}",
        learner_scope_id=f"learner-scope-{suffix}",
    )
    await repository.save_project(project)
    assert await repository.get_project(project_id) == project

    asset = Asset(
        id=f"asset-{suffix}",
        project_id=project_id,
        kind="code",
        title="train.py",
        uri=f"/demo/{suffix}/train.py",
        checksum="abc123",
        version="deadbeef",
        status="active",
    )
    await repository.save_asset(asset)
    assert await repository.get_asset(asset.id) == asset
    assert await repository.list_assets(project_id) == [asset]

    assessment_id = f"assessment-{suffix}"
    await repository.save_assessment(
        assessment_id,
        project_id,
        {"python": "intermediate", "nested": {"torch": True}},
    )
    stored_assessment = await repository.get_assessment(assessment_id)
    assert stored_assessment is not None
    assert stored_assessment["id"] == assessment_id
    assert stored_assessment["project_id"] == project_id
    assert stored_assessment["nested"] == {"torch": True}

    draft_id = f"handoff-draft-{suffix}"
    await repository.save_handoff_draft(
        draft_id,
        project_id,
        {"draft": {"objective": "baseline", "state": []}},
    )
    stored_draft = await repository.get_handoff_draft(draft_id)
    assert stored_draft is not None
    assert stored_draft["id"] == draft_id
    assert stored_draft["project_id"] == project_id
    assert stored_draft["draft"]["objective"] == "baseline"

    citation = Citation(
        asset_id=asset.id,
        kind="code",
        title=asset.title,
        uri=asset.uri,
        version=asset.version,
        locator={"line_start": 1, "line_end": 10},
    )
    task = LearningTask(
        id=f"task-{suffix}",
        project_id=project_id,
        plan_id=f"plan-{suffix}",
        stage=3,
        title="Read the training entrypoint",
        objective="Trace the baseline call chain",
        citations=[citation],
        completion_criteria="Explain the call chain",
    )
    second_task = task.model_copy(
        update={"id": f"task-early-{suffix}", "stage": 1, "title": "Set up the environment"}
    )
    plan = LearningPlan(
        id=task.plan_id,
        project_id=project_id,
        version=1,
        status="active",
        assessment_id=assessment_id,
        # saved out of stage order on purpose: the plan's own order is the contract
        tasks=[task, second_task],
    )
    await repository.save_plan(plan)
    assert await repository.get_plan(plan.id) == plan
    assert await repository.get_next_plan_version(project_id) == 2

    active_project = project.model_copy(
        update={
            "active_plan_id": plan.id,
            "handoff_ref": '{"family":"handoff","artifact_id":"h1","revision":1}',
            "handoff_receipt_ref": '{"name":"handoff-receipt","source_id":"ack-1"}',
        }
    )
    await repository.save_project(active_project)
    assert await repository.get_project(project_id) == active_project
    assert await repository.get_active_plan(project_id) == plan

    updated_task = task.model_copy(update={"status": "todo"})
    await repository.save_task(updated_task)
    assert await repository.get_task(task.id) == updated_task
    stored_plan = await repository.get_plan(plan.id)
    assert stored_plan is not None
    assert [item.id for item in stored_plan.tasks] == [task.id, second_task.id]
    assert stored_plan.tasks[0].status == "todo"

    first_event = TaskEvent(
        id=f"event-{suffix}",
        project_id=project_id,
        plan_id=plan.id,
        task_id=task.id,
        event_type="status_changed",
        payload={"from": "locked", "to": "todo"},
        occurred_at="2026-10-28T00:00:00+00:00",
    )
    second_event = first_event.model_copy(update={"id": f"event-b-{suffix}"})
    await repository.append_event(first_event)
    await repository.append_event(second_event)
    # one timestamp for both rows: only a monotonic sequence keeps the append order
    assert await repository.list_events(project_id) == [first_event, second_event]

    with pytest.raises(DuplicateAssetLocationError):
        await repository.save_asset(
            asset.model_copy(update={"id": f"asset-clash-{suffix}", "checksum": "abc123"})
        )
