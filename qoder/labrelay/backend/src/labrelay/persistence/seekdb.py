"""seekdb-backed ``Repository`` for LabRelay projects, plans, tasks and events."""

from __future__ import annotations

import json
import re
from contextlib import closing
from importlib.resources import files
from typing import Any

import anyio
import pymysql
from pymysql.cursors import DictCursor

from labrelay.config import LabRelayConfig
from labrelay.domain.models import Asset, LearningPlan, LearningTask, Project, TaskEvent

_DATABASE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")

_UPSERT_PROJECT = """
INSERT INTO projects
  (id, title, research_area, description, project_scope_id, learner_scope_id, active_plan_id)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
  title = VALUES(title),
  research_area = VALUES(research_area),
  description = VALUES(description),
  project_scope_id = VALUES(project_scope_id),
  learner_scope_id = VALUES(learner_scope_id),
  active_plan_id = VALUES(active_plan_id)
"""

_SELECT_PROJECT = """
SELECT id, title, research_area, description, project_scope_id, learner_scope_id, active_plan_id
FROM projects
WHERE id = %s
"""

_UPSERT_ASSET = """
INSERT INTO assets
  (id, project_id, kind, title, uri, checksum, version, status)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
  project_id = VALUES(project_id),
  kind = VALUES(kind),
  title = VALUES(title),
  uri = VALUES(uri),
  checksum = VALUES(checksum),
  version = VALUES(version),
  status = VALUES(status)
"""

_SELECT_ASSET = """
SELECT id, project_id, kind, title, uri, checksum, version, status
FROM assets
WHERE id = %s
"""

_SELECT_ASSETS = """
SELECT id, project_id, kind, title, uri, checksum, version, status
FROM assets
WHERE project_id = %s
ORDER BY created_at, id
"""

_UPSERT_ASSESSMENT = """
INSERT INTO assessments (id, project_id, payload_json)
VALUES (%s, %s, %s)
ON DUPLICATE KEY UPDATE
  project_id = VALUES(project_id),
  payload_json = VALUES(payload_json)
"""

_SELECT_ASSESSMENT = """
SELECT id, project_id, payload_json
FROM assessments
WHERE id = %s
"""

_UPSERT_PLAN = """
INSERT INTO plans
  (id, project_id, version, status, assessment_id, handoff_ref)
VALUES (%s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
  project_id = VALUES(project_id),
  version = VALUES(version),
  status = VALUES(status),
  assessment_id = VALUES(assessment_id),
  handoff_ref = VALUES(handoff_ref)
"""

_SELECT_PLAN = """
SELECT id, project_id, version, status, assessment_id
FROM plans
WHERE id = %s
"""

_SELECT_PLAN_TASKS = """
SELECT payload_json
FROM tasks
WHERE plan_id = %s
ORDER BY stage, position, id
"""

_SELECT_NEXT_PLAN_VERSION = """
SELECT COALESCE(MAX(version), 0) + 1 AS next_version
FROM plans
WHERE project_id = %s
"""

_UPSERT_TASK = """
INSERT INTO tasks
  (id, plan_id, project_id, stage, title, objective, status, task_type,
   estimated_minutes, position, payload_json)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
  plan_id = VALUES(plan_id),
  project_id = VALUES(project_id),
  stage = VALUES(stage),
  title = VALUES(title),
  objective = VALUES(objective),
  status = VALUES(status),
  task_type = VALUES(task_type),
  estimated_minutes = VALUES(estimated_minutes),
  position = VALUES(position),
  payload_json = VALUES(payload_json),
  updated_at = CURRENT_TIMESTAMP
"""

_UPSERT_TASK_KEEP_POSITION = """
INSERT INTO tasks
  (id, plan_id, project_id, stage, title, objective, status, task_type,
   estimated_minutes, position, payload_json)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
  plan_id = VALUES(plan_id),
  project_id = VALUES(project_id),
  stage = VALUES(stage),
  title = VALUES(title),
  objective = VALUES(objective),
  status = VALUES(status),
  task_type = VALUES(task_type),
  estimated_minutes = VALUES(estimated_minutes),
  payload_json = VALUES(payload_json),
  updated_at = CURRENT_TIMESTAMP
"""

_SELECT_TASK = """
SELECT payload_json
FROM tasks
WHERE id = %s
"""

_INSERT_EVENT = """
INSERT INTO task_events
  (id, project_id, plan_id, task_id, event_type, payload_json, occurred_at)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""

_SELECT_EVENTS = """
SELECT id, project_id, plan_id, task_id, event_type, payload_json, occurred_at
FROM task_events
WHERE project_id = %s
ORDER BY occurred_at, id
"""


class SeekdbRepository:
    """SQL-backed implementation of :class:`labrelay.domain.ports.Repository`."""

    def __init__(self, *, host: str, port: int, database: str, user: str, password: str) -> None:
        if not _DATABASE_IDENTIFIER.fullmatch(database):
            raise ValueError(f"Invalid seekdb database name: {database!r}")
        self._host = host
        self._port = port
        self._database = database
        self._user = user
        self._password = password

    @classmethod
    def from_config(cls, config: LabRelayConfig) -> SeekdbRepository:
        return cls(
            host=config.seekdb_host,
            port=config.seekdb_port,
            database=config.seekdb_database,
            user=config.seekdb_user,
            password=config.seekdb_password,
        )

    async def initialize(self) -> None:
        await anyio.to_thread.run_sync(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with closing(self._connect(with_database=False)) as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"CREATE DATABASE IF NOT EXISTS `{self._database}` CHARACTER SET utf8mb4"
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        for statement in _schema_statements():
            self._execute(statement)

    def _connect(self, *, with_database: bool = True) -> Any:
        return pymysql.connect(
            host=self._host,
            port=self._port,
            user=self._user,
            password=self._password,
            database=self._database if with_database else None,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=False,
            connect_timeout=5,
            read_timeout=10,
            write_timeout=10,
        )

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with closing(self._connect()) as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(sql, params)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                return cursor.fetchone()

    def _fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                return list(cursor.fetchall())

    async def save_project(self, project: Project) -> None:
        await anyio.to_thread.run_sync(self._save_project_sync, project)

    def _save_project_sync(self, project: Project) -> None:
        self._execute(
            _UPSERT_PROJECT,
            (
                project.id,
                project.title,
                project.research_area,
                project.description,
                project.project_scope_id,
                project.learner_scope_id,
                project.active_plan_id,
            ),
        )

    async def get_project(self, project_id: str) -> Project | None:
        return await anyio.to_thread.run_sync(self._get_project_sync, project_id)

    def _get_project_sync(self, project_id: str) -> Project | None:
        row = self._fetch_one(_SELECT_PROJECT, (project_id,))
        return Project(**row) if row is not None else None

    async def save_asset(self, asset: Asset) -> None:
        await anyio.to_thread.run_sync(self._save_asset_sync, asset)

    def _save_asset_sync(self, asset: Asset) -> None:
        self._execute(
            _UPSERT_ASSET,
            (
                asset.id,
                asset.project_id,
                asset.kind,
                asset.title,
                asset.uri,
                asset.checksum,
                asset.version,
                asset.status,
            ),
        )

    async def get_asset(self, asset_id: str) -> Asset | None:
        return await anyio.to_thread.run_sync(self._get_asset_sync, asset_id)

    def _get_asset_sync(self, asset_id: str) -> Asset | None:
        row = self._fetch_one(_SELECT_ASSET, (asset_id,))
        return Asset(**row) if row is not None else None

    async def list_assets(self, project_id: str) -> list[Asset]:
        return await anyio.to_thread.run_sync(self._list_assets_sync, project_id)

    def _list_assets_sync(self, project_id: str) -> list[Asset]:
        return [Asset(**row) for row in self._fetch_all(_SELECT_ASSETS, (project_id,))]

    async def save_assessment(
        self, assessment_id: str, project_id: str, payload: dict[str, object]
    ) -> None:
        await anyio.to_thread.run_sync(
            self._save_assessment_sync, assessment_id, project_id, payload
        )

    def _save_assessment_sync(
        self, assessment_id: str, project_id: str, payload: dict[str, object]
    ) -> None:
        stored = {**payload, "id": assessment_id, "project_id": project_id}
        self._execute(
            _UPSERT_ASSESSMENT,
            (
                assessment_id,
                project_id,
                json.dumps(stored, ensure_ascii=False, default=str),
            ),
        )

    async def get_assessment(self, assessment_id: str) -> dict[str, object] | None:
        return await anyio.to_thread.run_sync(self._get_assessment_sync, assessment_id)

    def _get_assessment_sync(self, assessment_id: str) -> dict[str, object] | None:
        row = self._fetch_one(_SELECT_ASSESSMENT, (assessment_id,))
        if row is None:
            return None
        parsed = json.loads(row["payload_json"])
        return parsed if isinstance(parsed, dict) else None

    async def save_plan(self, plan: LearningPlan) -> None:
        await anyio.to_thread.run_sync(self._save_plan_sync, plan)

    def _save_plan_sync(self, plan: LearningPlan) -> None:
        with closing(self._connect()) as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        _UPSERT_PLAN,
                        (
                            plan.id,
                            plan.project_id,
                            plan.version,
                            plan.status,
                            plan.assessment_id,
                            getattr(plan, "handoff_ref", None),
                        ),
                    )
                    for position, task in enumerate(plan.tasks):
                        cursor.execute(_UPSERT_TASK, _task_params(task, position))
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    async def get_plan(self, plan_id: str) -> LearningPlan | None:
        return await anyio.to_thread.run_sync(self._get_plan_sync, plan_id)

    def _get_plan_sync(self, plan_id: str) -> LearningPlan | None:
        row = self._fetch_one(_SELECT_PLAN, (plan_id,))
        if row is None:
            return None
        task_rows = self._fetch_all(_SELECT_PLAN_TASKS, (plan_id,))
        return LearningPlan(
            id=row["id"],
            project_id=row["project_id"],
            version=int(row["version"]),
            status=row["status"],
            assessment_id=row["assessment_id"],
            tasks=[
                LearningTask.model_validate_json(task_row["payload_json"]) for task_row in task_rows
            ],
        )

    async def get_active_plan(self, project_id: str) -> LearningPlan | None:
        return await anyio.to_thread.run_sync(self._get_active_plan_sync, project_id)

    def _get_active_plan_sync(self, project_id: str) -> LearningPlan | None:
        project = self._fetch_one(_SELECT_PROJECT, (project_id,))
        if project is None or project["active_plan_id"] is None:
            return None
        return self._get_plan_sync(project["active_plan_id"])

    async def get_next_plan_version(self, project_id: str) -> int:
        return await anyio.to_thread.run_sync(self._get_next_plan_version_sync, project_id)

    def _get_next_plan_version_sync(self, project_id: str) -> int:
        row = self._fetch_one(_SELECT_NEXT_PLAN_VERSION, (project_id,))
        return int(row["next_version"]) if row is not None else 1

    async def save_task(self, task: LearningTask) -> None:
        await anyio.to_thread.run_sync(self._save_task_sync, task)

    def _save_task_sync(self, task: LearningTask) -> None:
        self._execute(_UPSERT_TASK_KEEP_POSITION, _task_params(task, 0))

    async def get_task(self, task_id: str) -> LearningTask | None:
        return await anyio.to_thread.run_sync(self._get_task_sync, task_id)

    def _get_task_sync(self, task_id: str) -> LearningTask | None:
        row = self._fetch_one(_SELECT_TASK, (task_id,))
        return LearningTask.model_validate_json(row["payload_json"]) if row is not None else None

    async def append_event(self, event: TaskEvent) -> None:
        await anyio.to_thread.run_sync(self._append_event_sync, event)

    def _append_event_sync(self, event: TaskEvent) -> None:
        self._execute(
            _INSERT_EVENT,
            (
                event.id,
                event.project_id,
                event.plan_id,
                event.task_id,
                event.event_type,
                json.dumps(event.payload, ensure_ascii=False, default=str),
                event.occurred_at,
            ),
        )

    async def list_events(self, project_id: str) -> list[TaskEvent]:
        return await anyio.to_thread.run_sync(self._list_events_sync, project_id)

    def _list_events_sync(self, project_id: str) -> list[TaskEvent]:
        return [
            TaskEvent(
                id=row["id"],
                project_id=row["project_id"],
                plan_id=row["plan_id"],
                task_id=row["task_id"],
                event_type=row["event_type"],
                payload=json.loads(row["payload_json"]),
                occurred_at=row["occurred_at"],
            )
            for row in self._fetch_all(_SELECT_EVENTS, (project_id,))
        ]


def _task_params(task: LearningTask, position: int) -> tuple[Any, ...]:
    return (
        task.id,
        task.plan_id,
        task.project_id,
        task.stage,
        task.title,
        task.objective,
        task.status,
        task.task_type,
        task.estimated_minutes,
        position,
        task.model_dump_json(),
    )


def _schema_statements() -> list[str]:
    sql = files("labrelay.seekdb").joinpath("schema.sql").read_text(encoding="utf-8")
    return [statement.strip() for statement in sql.split(";") if statement.strip()]
