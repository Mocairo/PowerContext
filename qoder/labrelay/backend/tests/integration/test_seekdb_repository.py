import os
from contextlib import closing
from uuid import uuid4

import pymysql
import pytest
from pymysql.cursors import DictCursor

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


async def test_initialize_upgrades_a_database_created_before_the_handoff_columns() -> None:
    """A database laid down by the Task 7 schema has no handoff columns yet.

    ``initialize`` must add them idempotently, otherwise the first project write
    after an upgrade fails with "Unknown column 'handoff_ref'".
    """
    suffix = uuid4().hex[:8]
    database = f"labrelay_upgrade_{suffix}"
    config = _config().model_copy(update={"seekdb_database": database})
    with closing(
        pymysql.connect(
            host=config.seekdb_host,
            port=config.seekdb_port,
            user=config.seekdb_user,
            password=config.seekdb_password,
            charset="utf8mb4",
            cursorclass=DictCursor,
        )
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4")
            cursor.execute(f"USE `{database}`")
            cursor.execute(
                """
                CREATE TABLE projects (
                  id VARCHAR(64) PRIMARY KEY,
                  title VARCHAR(255) NOT NULL,
                  research_area VARCHAR(255) NOT NULL,
                  description TEXT NOT NULL,
                  project_scope_id VARCHAR(255) NOT NULL,
                  learner_scope_id VARCHAR(255) NOT NULL,
                  active_plan_id VARCHAR(64) NULL,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        connection.commit()

    repository = SeekdbRepository.from_config(config)
    await repository.initialize()
    await repository.initialize()

    project = Project(
        id=f"project-{suffix}",
        title="Upgraded project",
        research_area="Deep learning",
        description="A project stored before the handoff columns existed.",
        project_scope_id=f"project-scope-{suffix}",
        learner_scope_id=f"learner-scope-{suffix}",
        handoff_ref='{"family":"handoff","artifact_id":"h1","revision":1}',
        handoff_receipt_ref='{"name":"handoff-receipt","source_id":"ack-1"}',
    )
    await repository.save_project(project)
    assert await repository.get_project(project.id) == project


OLD_ASSETS_SQL = """
CREATE TABLE assets (
  id VARCHAR(64) NOT NULL,
  project_id VARCHAR(64) NOT NULL,
  kind VARCHAR(32) NOT NULL,
  title VARCHAR(512) NOT NULL,
  uri TEXT NOT NULL,
  checksum VARCHAR(128) NOT NULL,
  version VARCHAR(255) NULL,
  status VARCHAR(32) NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_asset_location (project_id, uri(191), checksum)
)
"""

OLD_EVENTS_SQL = """
CREATE TABLE task_events (
  id VARCHAR(64) NOT NULL,
  project_id VARCHAR(64) NOT NULL,
  plan_id VARCHAR(64) NOT NULL,
  task_id VARCHAR(64) NOT NULL,
  event_type VARCHAR(64) NOT NULL,
  payload_json TEXT NOT NULL,
  occurred_at VARCHAR(64) NOT NULL,
  PRIMARY KEY (id),
  INDEX idx_events_task (task_id)
)
"""


async def test_initialize_backfills_the_sequence_column_of_an_older_database() -> None:
    """Ordered reads run on ``seq``, so a database predating that column fails on every read.

    Found by running this suite against a live seekdb: the stored rows were fine, but
    ``list_assets`` died with ``Unknown column 'seq' in 'order clause'``, which no offline fake
    can report because the fake keeps rows in insertion order regardless of schema.
    """
    suffix = uuid4().hex[:8]
    database = f"labrelay_seq_{suffix}"
    config = _config().model_copy(update={"seekdb_database": database})
    with closing(
        pymysql.connect(
            host=config.seekdb_host,
            port=config.seekdb_port,
            user=config.seekdb_user,
            password=config.seekdb_password,
            charset="utf8mb4",
            cursorclass=DictCursor,
        )
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4")
            cursor.execute(f"USE `{database}`")
            cursor.execute(OLD_ASSETS_SQL)
            cursor.execute(OLD_EVENTS_SQL)
        connection.commit()

    repository = SeekdbRepository.from_config(config)
    await repository.initialize()
    await repository.initialize()

    project_id = f"project-{suffix}"
    for index in (1, 2, 3):
        await repository.save_asset(
            Asset(
                id=f"asset-{suffix}-{index}",
                project_id=project_id,
                kind="code",
                title=f"file{index}.py",
                uri=f"/demo/{suffix}/file{index}.py",
                checksum=f"sum-{suffix}-{index}",
                version="deadbeef",
                status="active",
            )
        )
    for index in (1, 2):
        await repository.append_event(
            TaskEvent(
                id=f"event-{suffix}-{index}",
                project_id=project_id,
                plan_id=f"plan-{suffix}",
                task_id=f"task-{suffix}",
                event_type="status_changed",
                payload={"index": index},
                occurred_at=f"2026-10-29T0{index}:00:00Z",
            )
        )

    assets = await repository.list_assets(project_id)
    assert [asset.title for asset in assets] == ["file1.py", "file2.py", "file3.py"]
    events = await repository.list_events(project_id)
    assert [event.id for event in events] == [f"event-{suffix}-1", f"event-{suffix}-2"]

    with closing(
        pymysql.connect(
            host=config.seekdb_host,
            port=config.seekdb_port,
            user=config.seekdb_user,
            password=config.seekdb_password,
            charset="utf8mb4",
            autocommit=True,
        )
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE `{database}`")
