"""Offline tests for ``SeekdbRepository`` driven by a fake PyMySQL connection."""

import asyncio
import re
from collections import defaultdict
from copy import deepcopy

import pymysql
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
from labrelay.persistence import seekdb as seekdb_module
from labrelay.persistence.memory import InMemoryRepository
from labrelay.persistence.seekdb import (
    DuplicateAssetLocationError,
    DuplicatePlanVersionError,
    SeekdbIntegrityError,
    SeekdbRepository,
)

TABLES = (
    "projects",
    "assets",
    "assessments",
    "handoff_drafts",
    "plans",
    "tasks",
    "task_events",
)

#: The non-primary uniqueness the shipped schema declares, with the key names MySQL reports.
UNIQUE_KEYS = {
    "assets": ("uq_asset_location", ("project_id", "uri", "checksum")),
    "plans": ("uq_project_plan_version", ("project_id", "version")),
}
#: Tables whose row order has to survive a same-timestamp write.
AUTO_INCREMENT = {"assets": "seq", "task_events": "seq"}

_INSERT_PATTERN = re.compile(r"INSERT INTO (\w+) \(([^)]+)\) VALUES \(([^)]+)\)")
_UPDATE_PATTERN = re.compile(r"UPDATE (\w+) SET (.+) WHERE (\w+) = %s")
_AGGREGATE_PATTERN = re.compile(
    r"SELECT COALESCE\((MAX\(\w+\)), -?\d+\) \+ 1 AS (\w+) FROM (\w+) WHERE (\w+) = %s"
)
_SELECT_PATTERN = re.compile(r"SELECT (.+?) FROM (\w+)(?: WHERE (\w+) = %s)?(?: ORDER BY (.+))?")


class FakeDatabase:
    """A statement-interpreting stand-in for the six seekdb tables."""

    def __init__(self) -> None:
        self.tables: dict[str, list[dict]] = {table: [] for table in TABLES}
        self.counters: dict[str, int] = defaultdict(int)
        self.statements: list[str] = []
        self.connect_kwargs: list[dict] = []
        self.closed = 0
        self.commits = 0
        self.rollbacks = 0
        self.fail_on: tuple[str, Exception] | None = None

    def connect(self, **kwargs) -> "FakeConnection":
        self.connect_kwargs.append(kwargs)
        return FakeConnection(self)

    def snapshot(self) -> dict[str, list[dict]]:
        return deepcopy(self.tables)

    def restore(self, snapshot: dict[str, list[dict]]) -> None:
        self.tables = deepcopy(snapshot)

    def rows(self, table: str) -> list[dict]:
        return self.tables[table]

    # -- statement dispatch ------------------------------------------------------

    def execute(self, sql: str, params: tuple) -> tuple[list[dict], int]:
        statement = " ".join(sql.split())
        self.statements.append(statement)
        if self.fail_on is not None and self.fail_on[0] in statement:
            raise self.fail_on[1]
        if statement.startswith(("CREATE DATABASE", "CREATE TABLE")):
            return [], 0
        if statement.startswith("INSERT"):
            return self._insert(statement, params)
        if statement.startswith("UPDATE"):
            return self._update(statement, params)
        if statement.startswith("SELECT"):
            return self._select(statement, params)
        raise AssertionError(f"unsupported statement: {statement}")

    def _insert(self, statement: str, params: tuple) -> tuple[list[dict], int]:
        match = _INSERT_PATTERN.match(statement)
        if match is None:
            raise AssertionError(f"unsupported INSERT: {statement}")
        table, columns = match.group(1), match.group(2)
        names = [column.strip() for column in columns.split(",")]
        row = dict(zip(names, params, strict=True))
        rows = self.tables[table]
        if any(existing["id"] == row["id"] for existing in rows):
            raise pymysql.IntegrityError(1062, "Duplicate entry for key 'PRIMARY'")
        if table in UNIQUE_KEYS:
            key_name, fields = UNIQUE_KEYS[table]
            if any(all(existing[field] == row[field] for field in fields) for existing in rows):
                raise pymysql.IntegrityError(1062, f"Duplicate entry for key '{key_name}'")
        self.counters[table] += 1
        column = AUTO_INCREMENT.get(table)
        if column is not None:
            row[column] = self.counters[table]
        else:
            row.setdefault("created_at", self.counters[table])
        rows.append(row)
        return [], 1

    def _update(self, statement: str, params: tuple) -> tuple[list[dict], int]:
        table, assignments, where_column = _UPDATE_PATTERN.match(statement).groups()
        set_columns = [part.split("=")[0].strip() for part in assignments.split(",")]
        updates = dict(zip(set_columns, params[: len(set_columns)], strict=True))
        updated = 0
        for row in self.tables[table]:
            if row[where_column] != params[-1]:
                continue
            if all(row[key] == value for key, value in updates.items()):
                # MySQL reports zero affected rows when an UPDATE rewrites identical values.
                return [], 0
            row.update(updates)
            updated += 1
        return [], updated

    def _select(self, statement: str, params: tuple) -> tuple[list[dict], int]:
        aggregate = _AGGREGATE_PATTERN.match(statement)
        if aggregate is not None:
            function, alias, table, filter_column = aggregate.groups()
            scoped = [row for row in self.tables[table] if row[filter_column] == params[0]]
            if "MAX(version)" in function:
                return [{alias: max((row["version"] for row in scoped), default=0) + 1}], 1
            return [{alias: max((row["position"] for row in scoped), default=-1) + 1}], 1

        match = _SELECT_PATTERN.match(statement)
        if match is None:
            raise AssertionError(f"unsupported SELECT: {statement}")
        columns, table, filter_column, order_by = match.groups()
        rows = self.tables[table]
        if filter_column is not None:
            rows = [row for row in rows if row[filter_column] == params[0]]
        if order_by is not None:
            keys = [key.strip() for key in order_by.split(",")]
            rows = sorted(rows, key=lambda row: tuple(row[key] for key in keys))
        names = [column.strip() for column in columns.split(",")]
        return [{name: row[name] for name in names} for row in rows], len(rows)


class FakeCursor:
    def __init__(self, database: FakeDatabase, connection: "FakeConnection") -> None:
        self.database = database
        self.connection = connection
        self.rowcount = 0
        self._rows: list[dict] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.connection.before_write(sql)
        self._rows, self.rowcount = self.database.execute(sql, tuple(params))

    def fetchone(self) -> dict | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict]:
        return list(self._rows)

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc_info) -> None:
        return None


class FakeConnection:
    def __init__(self, database: FakeDatabase) -> None:
        self.database = database
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self._snapshot: dict[str, list[dict]] | None = None

    def before_write(self, sql: str) -> None:
        if sql.strip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE")):
            if self._snapshot is None:
                self._snapshot = self.database.snapshot()

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.database, self)

    def commit(self) -> None:
        self.commits += 1
        self.database.commits += 1
        self._snapshot = None

    def rollback(self) -> None:
        self.rollbacks += 1
        self.database.rollbacks += 1
        if self._snapshot is not None:
            self.database.restore(self._snapshot)
            self._snapshot = None

    def close(self) -> None:
        self.closed = True
        self.database.closed += 1


@pytest.fixture
def database(monkeypatch) -> FakeDatabase:
    fake = FakeDatabase()
    monkeypatch.setattr(seekdb_module.pymysql, "connect", fake.connect)
    return fake


def _repository(**overrides) -> SeekdbRepository:
    settings = {
        "host": "db.internal",
        "port": 2881,
        "database": "labrelay",
        "user": "root",
        "password": "secret",
        "tenant": "labrelay_tenant",
    }
    settings.update(overrides)
    return SeekdbRepository(**settings)


def _config() -> LabRelayConfig:
    return LabRelayConfig(
        seekdb_host="db.internal",
        seekdb_port=2881,
        seekdb_database="labrelay",
        seekdb_user="root",
        seekdb_password="secret",
    )


def _run(coro):
    return asyncio.run(coro)


def _project(project_id: str = "project-1", title: str = "Aurora handoff", **overrides) -> Project:
    return Project(
        id=project_id,
        title=title,
        research_area="Deep learning",
        description="A synthetic project.",
        project_scope_id=f"{project_id}-scope",
        learner_scope_id=f"{project_id}-learner",
        **overrides,
    )


def _asset(
    asset_id: str = "asset-1",
    project_id: str = "project-1",
    uri: str | None = None,
    status: str = "active",
    checksum: str | None = None,
) -> Asset:
    return Asset(
        id=asset_id,
        project_id=project_id,
        kind="code",
        title="train.py",
        uri=uri if uri is not None else f"/demo/{asset_id}/train.py",
        checksum=checksum if checksum is not None else f"sum-{asset_id}",
        version="deadbeef",
        status=status,
    )


def _citation(asset_id: str = "asset-1") -> Citation:
    return Citation(
        asset_id=asset_id,
        kind="code",
        title="train.py",
        uri="/demo/train.py",
        version="deadbeef",
        locator={"line_start": 1},
    )


def _task(task_id: str, *, stage: int = 1, plan_id: str = "plan-1") -> LearningTask:
    return LearningTask(
        id=task_id,
        project_id="project-1",
        plan_id=plan_id,
        stage=stage,
        title=f"Task {task_id}",
        objective="Trace the baseline call chain",
        citations=[_citation()],
        completion_criteria="Explain the call chain",
    )


def _plan(tasks: list[LearningTask], *, version: int = 1, plan_id: str = "plan-1") -> LearningPlan:
    return LearningPlan(
        id=plan_id,
        project_id="project-1",
        version=version,
        status="active",
        assessment_id="assessment-1",
        tasks=tasks,
    )


def _event(event_id: str, *, project_id: str = "project-1") -> TaskEvent:
    return TaskEvent(
        id=event_id,
        project_id=project_id,
        plan_id="plan-1",
        task_id="task-1",
        event_type="status_changed",
        payload={"from": "locked", "to": "todo"},
        occurred_at="2026-10-28T00:00:00+00:00",
    )


# ---------------------------------------------------------- schema and connect


def test_initialize_creates_the_database_and_every_table(database) -> None:
    repository = _repository()

    _run(repository.initialize())

    creates = [statement for statement in database.statements if statement.startswith("CREATE")]
    assert creates[0].startswith("CREATE DATABASE IF NOT EXISTS `labrelay`")
    assert len(creates) == 1 + len(TABLES)
    for table in TABLES:
        assert any(f"CREATE TABLE IF NOT EXISTS {table}" in statement for statement in creates)
    assert database.commits == 2
    assert database.closed == 2


def test_connection_carries_the_tenant_and_bounded_timeouts(database) -> None:
    repository = _repository()

    _run(repository.get_project("project-1"))

    kwargs = database.connect_kwargs[0]
    assert kwargs["user"] == "root@labrelay_tenant"
    assert kwargs["database"] == "labrelay"
    assert kwargs["autocommit"] is False
    assert kwargs["connect_timeout"] == 5
    assert kwargs["read_timeout"] == 10
    assert kwargs["write_timeout"] == 10


def test_an_already_qualified_user_is_not_suffixed_twice(database) -> None:
    repository = _repository(user="root@sys", tenant="sys")

    _run(repository.get_project("project-1"))

    assert database.connect_kwargs[0]["user"] == "root@sys"


def test_from_config_reads_the_tenant(database) -> None:
    config = LabRelayConfig(
        seekdb_host="db.internal",
        seekdb_port=2881,
        seekdb_database="labrelay",
        seekdb_user="root",
        seekdb_password="secret",
        seekdb_tenant="labrelay_tenant",
    )

    assert SeekdbRepository.from_config(config)._tenant == "labrelay_tenant"


# ------------------------------------------------------------ PK-based saves


def test_save_project_inserts_then_updates_by_primary_key(database) -> None:
    repository = _repository()

    _run(repository.save_project(_project()))
    _run(repository.save_project(_project(title="Aurora v2")))

    assert database.statements[-1].startswith("UPDATE projects SET")
    assert database.statements[-1].endswith("WHERE id = %s")
    assert len(database.rows("projects")) == 1
    assert _run(repository.get_project("project-1")).title == "Aurora v2"


def test_rewriting_identical_values_never_inserts_a_second_row(database) -> None:
    repository = _repository()

    _run(repository.save_project(_project()))
    _run(repository.save_project(_project()))

    inserts = [s for s in database.statements if s.startswith("INSERT INTO projects")]
    assert len(inserts) == 1
    assert len(database.rows("projects")) == 1


def test_asset_location_conflict_is_a_typed_integrity_error(database) -> None:
    repository = _repository()
    _run(repository.save_asset(_asset("asset-1")))

    # the same file re-imported under a new id: ON DUPLICATE KEY UPDATE would have rewritten
    # asset-1's primary key instead of telling anybody
    with pytest.raises(DuplicateAssetLocationError, match="uq_asset_location"):
        _run(
            repository.save_asset(
                _asset("asset-2", uri="/demo/asset-1/train.py", checksum="sum-asset-1")
            )
        )

    assert [row["id"] for row in database.rows("assets")] == ["asset-1"]
    assert _run(repository.get_asset("asset-2")) is None
    assert database.rollbacks == 1


def test_asset_is_rewritten_in_place_by_its_primary_key(database) -> None:
    repository = _repository()

    _run(repository.save_asset(_asset("asset-1")))
    _run(repository.save_asset(_asset("asset-1", status="restricted")))

    assert _run(repository.get_asset("asset-1")).status == "restricted"
    assert len(database.rows("assets")) == 1


def test_unrelated_integrity_failure_keeps_its_key_name(database) -> None:
    repository = _repository()
    database.fail_on = (
        "INSERT INTO projects",
        pymysql.IntegrityError(1062, "Duplicate entry for key 'uk_project_scope'"),
    )

    with pytest.raises(SeekdbIntegrityError, match="uk_project_scope"):
        _run(repository.save_project(_project()))


# ------------------------------------------------------------------ plan order


def test_get_plan_restores_the_exact_saved_task_order(database) -> None:
    repository = _repository()
    plan = _plan([_task("task-c", stage=3), _task("task-a", stage=1), _task("task-b", stage=2)])

    _run(repository.save_plan(plan))

    stored = _run(repository.get_plan("plan-1"))
    assert [task.id for task in stored.tasks] == ["task-c", "task-a", "task-b"]
    assert stored == plan
    assert "SELECT payload_json FROM tasks WHERE plan_id = %s ORDER BY position, id" in (
        database.statements
    )


def test_a_plan_is_saved_in_one_committed_transaction(database) -> None:
    repository = _repository()

    _run(repository.save_plan(_plan([_task("task-1"), _task("task-2")])))

    assert database.commits == 1
    assert database.rollbacks == 0
    assert [row["position"] for row in database.rows("tasks")] == [0, 1]


def test_a_failed_plan_transaction_rolls_back_without_partial_rows(database) -> None:
    repository = _repository()
    database.fail_on = ("INSERT INTO tasks", RuntimeError("disk full"))

    with pytest.raises(RuntimeError, match="disk full"):
        _run(repository.save_plan(_plan([_task("task-1"), _task("task-2")])))

    assert database.rows("plans") == []
    assert database.rows("tasks") == []
    assert database.rollbacks == 1
    assert database.commits == 0


def test_save_task_keeps_the_position_of_a_known_task(database) -> None:
    repository = _repository()
    _run(repository.save_plan(_plan([_task("task-1"), _task("task-2")])))
    database.statements.clear()

    _run(repository.save_task(_task("task-1", stage=9).model_copy(update={"status": "todo"})))

    assert not [s for s in database.statements if s.startswith("INSERT INTO tasks")]
    assert not [s for s in database.statements if "position = %s" in s]
    stored = _run(repository.get_plan("plan-1"))
    assert [task.id for task in stored.tasks] == ["task-1", "task-2"]
    assert stored.tasks[0].status == "todo"
    assert stored.tasks[0].stage == 9


def test_save_task_appends_a_task_no_plan_saved(database) -> None:
    repository = _repository()
    _run(repository.save_plan(_plan([_task("task-1"), _task("task-2")])))

    _run(repository.save_task(_task("task-3")))

    assert {row["id"]: row["position"] for row in database.rows("tasks")} == {
        "task-1": 0,
        "task-2": 1,
        "task-3": 2,
    }
    assert _run(repository.get_task("task-3")).id == "task-3"


def test_plan_version_collision_is_re_allocated_once(database) -> None:
    repository = _repository()
    _run(repository.save_plan(_plan([_task("task-1")], version=1, plan_id="plan-1")))

    # a concurrent replan took version 1 between get_next_plan_version() and this save
    _run(repository.save_plan(_plan([_task("task-2")], version=1, plan_id="plan-2")))

    assert _run(repository.get_plan("plan-2")).version == 2
    assert _run(repository.get_next_plan_version("project-1")) == 3


def test_an_unresolvable_version_collision_surfaces_as_a_typed_error(database) -> None:
    repository = _repository()
    database.fail_on = (
        "INSERT INTO plans",
        pymysql.IntegrityError(1062, "Duplicate entry for key 'uq_project_plan_version'"),
    )

    with pytest.raises(DuplicatePlanVersionError, match="uq_project_plan_version"):
        _run(repository.save_plan(_plan([_task("task-1")])))

    assert database.rollbacks == 1
    assert database.rows("plans") == []


# ------------------------------------------------------------------ and events


def test_events_come_back_in_append_order_for_one_timestamp(database) -> None:
    repository = _repository()

    for event_id in ("event-1", "event-2", "event-3"):
        _run(repository.append_event(_event(event_id)))

    events = _run(repository.list_events("project-1"))
    assert [event.id for event in events] == ["event-1", "event-2", "event-3"]
    assert events[0].payload == {"from": "locked", "to": "todo"}
    assert any(
        statement.startswith("SELECT id, project_id") and statement.endswith("ORDER BY seq")
        for statement in database.statements
    )


def test_events_are_project_scoped(database) -> None:
    repository = _repository()

    _run(repository.append_event(_event("event-1")))
    _run(repository.append_event(_event("event-2", project_id="project-2")))

    events = _run(repository.list_events("project-2"))
    assert [event.id for event in events] == ["event-2"]


def test_assets_come_back_in_saved_order_for_one_project_only(database) -> None:
    repository = _repository()

    _run(repository.save_asset(_asset("asset-b")))
    _run(repository.save_asset(_asset("asset-a")))
    _run(repository.save_asset(_asset("asset-c", project_id="project-2")))

    assets = _run(repository.list_assets("project-1"))

    assert [asset.id for asset in assets] == ["asset-b", "asset-a"]


def test_assessment_round_trip_overwrites_forged_identity_keys(database) -> None:
    repository = _repository()

    _run(
        repository.save_assessment(
            "assessment-1",
            "project-1",
            {"python": "intermediate", "id": "forged", "project_id": "project-2"},
        )
    )

    stored = _run(repository.get_assessment("assessment-1"))
    assert stored["id"] == "assessment-1"
    assert stored["project_id"] == "project-1"
    assert stored["python"] == "intermediate"
    assert _run(repository.get_assessment("missing")) is None


def test_get_active_plan_follows_the_project_pointer(database) -> None:
    repository = _repository()
    plan = _plan([_task("task-1")])

    _run(repository.save_plan(plan))
    _run(repository.save_project(_project(active_plan_id=plan.id)))

    assert _run(repository.get_active_plan("project-1")) == plan
    assert _run(repository.get_active_plan("project-unknown")) is None


# --------------------------------------------------- in-memory interchangeability


def test_handoff_draft_survives_a_reconnect_and_updates_by_primary_key(database) -> None:
    repository = _repository()

    _run(repository.save_handoff_draft("draft-1", "project-1", {"objective": "baseline"}))
    first = _run(repository.get_handoff_draft("draft-1"))
    _run(repository.save_handoff_draft("draft-1", "project-1", {"objective": "ablation"}))
    reconnect = _repository()
    second = _run(reconnect.get_handoff_draft("draft-1"))

    assert first == {"id": "draft-1", "project_id": "project-1", "objective": "baseline"}
    assert second == {"id": "draft-1", "project_id": "project-1", "objective": "ablation"}
    assert len(database.rows("handoff_drafts")) == 1


def test_seekdb_repository_reads_like_the_in_memory_repository(database) -> None:
    """The same scenario must produce the same reads through either side of the port."""
    tasks = [_task("task-late-stage", stage=3), _task("task-early-stage", stage=1)]
    plan = _plan(tasks)
    events = [_event("event-1"), _event("event-2"), _event("event-3")]

    async def scenario(repository):
        await repository.initialize()
        await repository.save_project(_project())
        await repository.save_asset(_asset("asset-b"))
        await repository.save_asset(_asset("asset-a"))
        await repository.save_assessment("assessment-1", "project-1", {"python": "advanced"})
        await repository.save_handoff_draft(
            "draft-1",
            "project-1",
            {"objective": "baseline", "citations": ["asset-a"]},
        )
        await repository.save_plan(plan)
        await repository.save_project(_project(active_plan_id="plan-1"))
        await repository.save_task(tasks[0].model_copy(update={"status": "todo"}))
        for event in events:
            await repository.append_event(event)
        return {
            "assets": [asset.id for asset in await repository.list_assets("project-1")],
            "plan": await repository.get_plan("plan-1"),
            "active": await repository.get_active_plan("project-1"),
            "task": await repository.get_task("task-late-stage"),
            "events": [event.id for event in await repository.list_events("project-1")],
            "assessment": await repository.get_assessment("assessment-1"),
            "handoff_draft": await repository.get_handoff_draft("draft-1"),
            "next_version": await repository.get_next_plan_version("project-1"),
            "missing": await repository.get_project("project-unknown"),
        }

    memory = _run(scenario(InMemoryRepository()))
    seekdb = _run(scenario(SeekdbRepository.from_config(_config())))

    assert seekdb == memory


def test_seekdb_repository_uses_the_tenant_from_config_without_rewriting_memory(database) -> None:
    repository = SeekdbRepository.from_config(LabRelayConfig())

    _run(repository.get_project("project-1"))

    assert database.connect_kwargs[0]["user"] == "root@sys"
    assert repository._tenant == "sys"
