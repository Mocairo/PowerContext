"""seekdb-backed ``Repository`` for LabRelay projects, plans, tasks and events.

Every write goes through a primary-key based update-then-insert rather than
``ON DUPLICATE KEY UPDATE``: the schema's non-primary uniqueness (an asset location, a
plan version) must never be silently folded into somebody else's row. A real constraint
violation is reported as :class:`SeekdbIntegrityError` instead.

Each operation opens its own pymysql connection and closes it again, so two concurrent
coroutines never share a connection, and ``__init__`` never contacts the database.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

import anyio
import pymysql
from pymysql.cursors import DictCursor

from labrelay.config import LabRelayConfig
from labrelay.domain.models import Asset, LearningPlan, LearningTask, Project, TaskEvent

_DATABASE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")
_INTEGRITY_KEY = re.compile(r"for key '([^']+)'")

DEFAULT_TENANT = "sys"
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 10
# A concurrent replan can take the version between get_next_plan_version() and the save.
_PLAN_VERSION_ATTEMPTS = 3

logger = logging.getLogger("labrelay.persistence.seekdb")


class SeekdbIntegrityError(RuntimeError):
    """seekdb refused a write because a declared constraint was violated."""

    def __init__(self, message: str, *, key: str | None = None) -> None:
        super().__init__(message)
        self.key = key


class DuplicateAssetLocationError(SeekdbIntegrityError):
    """Another asset already owns this ``(project_id, uri, checksum)`` location."""


class DuplicatePlanVersionError(SeekdbIntegrityError):
    """Another plan already carries this ``(project_id, version)`` pair."""


_KEY_ERRORS: dict[str, type[SeekdbIntegrityError]] = {
    "uq_asset_location": DuplicateAssetLocationError,
    "uq_project_plan_version": DuplicatePlanVersionError,
}


@dataclass(frozen=True)
class _Table:
    """The column lists and statements one table is written with."""

    name: str
    columns: tuple[str, ...]
    updatable: tuple[str, ...]
    insert_only: bool = False

    @property
    def insert(self) -> str:
        placeholders = ", ".join(["%s"] * len(self.columns))
        return f"INSERT INTO {self.name} ({', '.join(self.columns)}) VALUES ({placeholders})"

    def update(self, *skip: str) -> str:
        columns = tuple(column for column in self.updatable if column not in skip)
        assignments = ", ".join(f"{column} = %s" for column in columns)
        return f"UPDATE {self.name} SET {assignments} WHERE id = %s"

    @property
    def exists(self) -> str:
        return f"SELECT id FROM {self.name} WHERE id = %s"

    def select(self) -> str:
        return f"SELECT {', '.join(self.columns)} FROM {self.name} WHERE id = %s"


_PROJECTS = _Table(
    "projects",
    (
        "id",
        "title",
        "research_area",
        "description",
        "project_scope_id",
        "learner_scope_id",
        "active_plan_id",
    ),
    (
        "title",
        "research_area",
        "description",
        "project_scope_id",
        "learner_scope_id",
        "active_plan_id",
    ),
)
_ASSETS = _Table(
    "assets",
    ("id", "project_id", "kind", "title", "uri", "checksum", "version", "status"),
    ("project_id", "kind", "title", "uri", "checksum", "version", "status"),
)
_ASSESSMENTS = _Table(
    "assessments",
    ("id", "project_id", "payload_json"),
    ("project_id", "payload_json"),
)
_PLANS = _Table(
    "plans",
    ("id", "project_id", "version", "status", "assessment_id", "handoff_ref"),
    ("project_id", "version", "status", "assessment_id", "handoff_ref"),
)
_TASKS = _Table(
    "tasks",
    (
        "id",
        "plan_id",
        "project_id",
        "stage",
        "title",
        "objective",
        "status",
        "task_type",
        "estimated_minutes",
        "position",
        "payload_json",
    ),
    (
        "plan_id",
        "project_id",
        "stage",
        "title",
        "objective",
        "status",
        "task_type",
        "estimated_minutes",
        "position",
        "payload_json",
    ),
)
_EVENTS = _Table(
    "task_events",
    ("id", "project_id", "plan_id", "task_id", "event_type", "payload_json", "occurred_at"),
    (),
    insert_only=True,
)


def _select_scoped(table: _Table, scope_column: str, *order_by: str) -> str:
    order = f" ORDER BY {', '.join(order_by)}" if order_by else ""
    return f"SELECT {', '.join(table.columns)} FROM {table.name} WHERE {scope_column} = %s{order}"


_SELECT_ASSETS = _select_scoped(_ASSETS, "project_id", "seq")
_SELECT_EVENTS = _select_scoped(_EVENTS, "project_id", "seq")
_SELECT_PLAN_TASKS = "SELECT payload_json FROM tasks WHERE plan_id = %s ORDER BY position, id"
_SELECT_PLAN = "SELECT id, project_id, version, status, assessment_id FROM plans WHERE id = %s"
_SELECT_NEXT_PLAN_VERSION = (
    "SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM plans WHERE project_id = %s"
)
_SELECT_NEXT_TASK_POSITION = (
    "SELECT COALESCE(MAX(position), -1) + 1 AS next_position FROM tasks WHERE plan_id = %s"
)


class SeekdbRepository:
    """SQL-backed implementation of :class:`labrelay.domain.ports.Repository`."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        tenant: str = DEFAULT_TENANT,
    ) -> None:
        if not _DATABASE_IDENTIFIER.fullmatch(database):
            raise ValueError(f"Invalid seekdb database name: {database!r}")
        self._host = host
        self._port = port
        self._database = database
        self._user = user
        self._password = password
        self._tenant = tenant

    @classmethod
    def from_config(cls, config: LabRelayConfig) -> SeekdbRepository:
        return cls(
            host=config.seekdb_host,
            port=config.seekdb_port,
            database=config.seekdb_database,
            user=config.seekdb_user,
            password=config.seekdb_password,
            tenant=config.seekdb_tenant,
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

        with closing(self._connect()) as connection:
            try:
                with connection.cursor() as cursor:
                    for statement in _schema_statements():
                        cursor.execute(statement)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    # ------------------------------------------------------------- connections

    def _connect(self, *, with_database: bool = True) -> Any:
        return pymysql.connect(
            host=self._host,
            port=self._port,
            user=self._connection_user(),
            password=self._password,
            database=self._database if with_database else None,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=False,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            read_timeout=READ_TIMEOUT_SECONDS,
            write_timeout=READ_TIMEOUT_SECONDS,
        )

    def _connection_user(self) -> str:
        """seekdb addresses a tenant as ``user@tenant``; an already qualified user wins."""
        if not self._tenant or "@" in self._user:
            return self._user
        return f"{self._user}@{self._tenant}"

    def _transaction(self, write: Any) -> None:
        with closing(self._connect()) as connection:
            try:
                with connection.cursor() as cursor:
                    write(cursor)
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

    # ------------------------------------------------------------------- writes

    @staticmethod
    def _write(cursor: Any, sql: str, params: tuple[Any, ...]) -> None:
        try:
            cursor.execute(sql, params)
        except pymysql.IntegrityError as exc:
            raise _translate_integrity_error(exc, sql) from exc

    def _save_row(
        self,
        cursor: Any,
        table: _Table,
        values: dict[str, Any],
        *,
        skip: tuple[str, ...] = (),
        before_insert: Callable[[Any, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        """Update by primary key, insert only when the row really is new."""
        if table.insert_only:
            self._write(cursor, table.insert, tuple(values[column] for column in table.columns))
            return

        columns = tuple(column for column in table.updatable if column not in skip)
        params = tuple(values[column] for column in columns) + (values["id"],)
        self._write(cursor, table.update(*skip), params)
        if cursor.rowcount:
            return
        cursor.execute(table.exists, (values["id"],))
        if cursor.fetchone() is not None:
            # MySQL reports zero affected rows when an UPDATE rewrites identical values, so
            # a missing count never means the row is missing.
            return
        if before_insert is not None:
            values = before_insert(cursor, values)
        self._write(cursor, table.insert, tuple(values[column] for column in table.columns))

    async def save_project(self, project: Project) -> None:
        await anyio.to_thread.run_sync(self._save_project_sync, project)

    def _save_project_sync(self, project: Project) -> None:
        self._transaction(lambda cursor: self._save_row(cursor, _PROJECTS, project.model_dump()))

    async def get_project(self, project_id: str) -> Project | None:
        return await anyio.to_thread.run_sync(self._get_project_sync, project_id)

    def _get_project_sync(self, project_id: str) -> Project | None:
        row = self._fetch_one(_PROJECTS.select(), (project_id,))
        return Project(**row) if row is not None else None

    async def save_asset(self, asset: Asset) -> None:
        await anyio.to_thread.run_sync(self._save_asset_sync, asset)

    def _save_asset_sync(self, asset: Asset) -> None:
        self._transaction(lambda cursor: self._save_row(cursor, _ASSETS, asset.model_dump()))

    async def get_asset(self, asset_id: str) -> Asset | None:
        return await anyio.to_thread.run_sync(self._get_asset_sync, asset_id)

    def _get_asset_sync(self, asset_id: str) -> Asset | None:
        row = self._fetch_one(_ASSETS.select(), (asset_id,))
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
        # The identity the caller named wins over anything the payload claims about itself.
        stored = {**payload, "id": assessment_id, "project_id": project_id}
        values = {
            "id": assessment_id,
            "project_id": project_id,
            "payload_json": json.dumps(stored, ensure_ascii=False, default=str),
        }
        self._transaction(lambda cursor: self._save_row(cursor, _ASSESSMENTS, values))

    async def get_assessment(self, assessment_id: str) -> dict[str, object] | None:
        return await anyio.to_thread.run_sync(self._get_assessment_sync, assessment_id)

    def _get_assessment_sync(self, assessment_id: str) -> dict[str, object] | None:
        row = self._fetch_one(_ASSESSMENTS.select(), (assessment_id,))
        if row is None:
            return None
        parsed = json.loads(row["payload_json"])
        return parsed if isinstance(parsed, dict) else None

    async def save_plan(self, plan: LearningPlan) -> None:
        """Persist a plan and its tasks, retrying once over a concurrent replan.

        ``get_next_plan_version`` and this write are separate statements, so a second
        replan can claim the same version in between. The version is the only field the
        retry re-allocates, and a collision that cannot be resolved stays a
        :class:`DuplicatePlanVersionError`.
        """
        current = plan
        for attempt in range(_PLAN_VERSION_ATTEMPTS):
            try:
                await anyio.to_thread.run_sync(self._save_plan_sync, current)
                return
            except DuplicatePlanVersionError:
                next_version = await self.get_next_plan_version(plan.project_id)
                if attempt + 1 >= _PLAN_VERSION_ATTEMPTS or next_version <= current.version:
                    raise
                logger.warning(
                    "plan version collision operation=save_plan project_id=%s plan_id=%s "
                    "version=%s retry_version=%s",
                    plan.project_id,
                    plan.id,
                    current.version,
                    next_version,
                )
                current = current.model_copy(update={"version": next_version})

    def _save_plan_sync(self, plan: LearningPlan) -> None:
        def write(cursor: Any) -> None:
            self._save_row(cursor, _PLANS, _plan_values(plan))
            for position, task in enumerate(plan.tasks):
                self._save_row(cursor, _TASKS, _task_values(task, position))

        self._transaction(write)

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
        project = self._fetch_one(_PROJECTS.select(), (project_id,))
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
        values = _task_values(task, position=0)

        def append_position(cursor: Any, pending: dict[str, Any]) -> dict[str, Any]:
            # A task no plan saved yet appends itself behind the plan it claims to belong to.
            cursor.execute(_SELECT_NEXT_TASK_POSITION, (pending["plan_id"],))
            row = cursor.fetchone() or {}
            return {**pending, "position": int(row.get("next_position", 0))}

        # ``position`` is deliberately skipped: a progress write must not reorder the plan.
        self._transaction(
            lambda cursor: self._save_row(
                cursor, _TASKS, values, skip=("position",), before_insert=append_position
            )
        )

    async def get_task(self, task_id: str) -> LearningTask | None:
        return await anyio.to_thread.run_sync(self._get_task_sync, task_id)

    def _get_task_sync(self, task_id: str) -> LearningTask | None:
        row = self._fetch_one("SELECT payload_json FROM tasks WHERE id = %s", (task_id,))
        return LearningTask.model_validate_json(row["payload_json"]) if row is not None else None

    async def append_event(self, event: TaskEvent) -> None:
        await anyio.to_thread.run_sync(self._append_event_sync, event)

    def _append_event_sync(self, event: TaskEvent) -> None:
        values = {
            "id": event.id,
            "project_id": event.project_id,
            "plan_id": event.plan_id,
            "task_id": event.task_id,
            "event_type": event.event_type,
            "payload_json": json.dumps(event.payload, ensure_ascii=False, default=str),
            "occurred_at": event.occurred_at,
        }
        # Events are append-only: ``seq`` is assigned by the database, never by the caller.
        self._transaction(lambda cursor: self._save_row(cursor, _EVENTS, values))

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


def _translate_integrity_error(exc: pymysql.IntegrityError, sql: str) -> SeekdbIntegrityError:
    detail = str(exc.args[-1]) if exc.args else "constraint violated"
    key_match = _INTEGRITY_KEY.search(detail)
    key = key_match.group(1) if key_match else None
    error_type = _KEY_ERRORS.get(key or "", SeekdbIntegrityError)
    table = sql.split(" INTO ", 1)[1].split(" ", 1)[0] if " INTO " in sql else sql.split()[1]
    return error_type(f"seekdb rejected the write to {table}: {detail}", key=key)


def _plan_values(plan: LearningPlan) -> dict[str, Any]:
    return {
        "id": plan.id,
        "project_id": plan.project_id,
        "version": plan.version,
        "status": plan.status,
        "assessment_id": plan.assessment_id,
        "handoff_ref": getattr(plan, "handoff_ref", None),
    }


def _task_values(task: LearningTask, position: int) -> dict[str, Any]:
    return {
        "id": task.id,
        "plan_id": task.plan_id,
        "project_id": task.project_id,
        "stage": task.stage,
        "title": task.title,
        "objective": task.objective,
        "status": task.status,
        "task_type": task.task_type,
        "estimated_minutes": task.estimated_minutes,
        "position": position,
        "payload_json": task.model_dump_json(),
    }


def _schema_statements() -> list[str]:
    sql = files("labrelay.seekdb").joinpath("schema.sql").read_text(encoding="utf-8")
    return [statement.strip() for statement in sql.split(";") if statement.strip()]
