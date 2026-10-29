"""Task 5: the in-memory repository and the ``LabRelayService`` cases built on it.

Every test drives real collaborators — ``InMemoryRepository``, the real
``InMemorySearchEngine`` and the real importer — so the seams Tasks 6 and 7 rely on
(``service.repository``, ``service.engine`` and the port method surface) are exercised
as they will be used, not through mock-only assertions. ``ContextGateway`` is faked
because the PowerContext adapter is Task 9.
"""

import asyncio
import inspect
import logging
from pathlib import Path
from typing import Protocol

import pytest
import yaml

from labrelay.application.services import LabRelayService
from labrelay.config import PlannerConfig
from labrelay.domain.models import (
    ALLOWED_TASK_TRANSITIONS,
    Asset,
    Citation,
    LearningPlan,
    LearningTask,
    Project,
)
from labrelay.domain.ports import Repository, SearchHit
from labrelay.persistence.memory import InMemoryRepository
from labrelay.retrieval.in_memory import InMemorySearchEngine

PLANNER_CONFIG = Path(__file__).resolve().parents[3] / "configs" / "planner.yaml"
RETRIEVAL_CONFIG = Path(__file__).resolve().parents[3] / "configs" / "retrieval.yaml"
SERVICE_LOGGER = "labrelay.application.services"


class ConstantModel:
    """The plan's embedding stand-in: one fixed vector, so ranking stays lexical."""

    def embed(self, texts):
        return [[1.0] for _ in texts]


class RecordingContext:
    """A ``ContextGateway`` double that records what the service asked memory to hold."""

    def __init__(self) -> None:
        self.scoped_projects: list[Project] = []
        self.remembered: list[tuple[str, str, str, str | None]] = []

    async def ensure_scopes(self, project):
        self.scoped_projects.append(project)
        return f"project:{project.id}", f"learner:{project.id}"

    async def remember(self, scope_id, kind, text, reason=None):
        self.remembered.append((scope_id, kind, text, reason))

    async def prepare_context(self, scope_id, query, max_bytes):
        return None, None

    async def project_context_summary(self, project_scope_id, learner_scope_id, query):
        return None, "local_progress"

    async def health_check(self):
        return True


class ExplodingContext(RecordingContext):
    """PowerContext is unreachable: every memory write raises, progress must not."""

    message = "powercontext call failed api_key=super-secret-value"

    async def remember(self, scope_id, kind, text, reason=None):
        raise RuntimeError(self.message)


class BadScopeContext(RecordingContext):
    """A gateway that hands back empty or missing scope identifiers."""

    def __init__(self, scopes: tuple[object, object]) -> None:
        super().__init__()
        self.scopes = scopes

    async def ensure_scopes(self, project):
        self.scoped_projects.append(project)
        return self.scopes


def citation(asset_id: str = "asset-1", kind: str = "note") -> Citation:
    return Citation(
        asset_id=asset_id,
        kind=kind,
        title=f"{asset_id}.md",
        uri=f"repo/{asset_id}.md",
        version="commit-1",
        locator={"line_start": 1, "line_end": 2},
        excerpt="run the baseline sweep",
    )


def make_asset(asset_id: str, project_id: str) -> Asset:
    return Asset(
        id=asset_id,
        project_id=project_id,
        kind="note",
        title=f"{asset_id}.md",
        uri=f"repo/{asset_id}.md",
        checksum="x",
        status="active",
    )


def make_task(
    task_id: str = "task-1",
    *,
    project_id: str = "project-1",
    plan_id: str = "plan-1",
    status: str = "todo",
    **overrides: object,
) -> LearningTask:
    return LearningTask(
        id=task_id,
        project_id=project_id,
        plan_id=plan_id,
        stage=0,
        title="了解资源规范",
        objective="知道如何申请和释放 GPU",
        estimated_minutes=20,
        citations=[citation()],
        completion_criteria="能复述规则",
        status=status,
        **overrides,
    )


def make_plan(*tasks: LearningTask, plan_id: str = "plan-1", **overrides: object) -> LearningPlan:
    return LearningPlan(
        id=plan_id,
        project_id=tasks[0].project_id,
        version=1,
        status="active",
        assessment_id="assessment-1",
        tasks=list(tasks),
        **overrides,
    )


def make_project(project_id: str = "project-1", **overrides: object) -> Project:
    return Project(
        id=project_id,
        title="Aurora",
        research_area="Deep Learning",
        description="Reproduce the baseline",
        project_scope_id=f"project:{project_id}",
        learner_scope_id=f"learner:{project_id}",
        **overrides,
    )


def make_service(
    *,
    repository: InMemoryRepository | None = None,
    engine: object | None = None,
    context: RecordingContext | None = None,
    model: ConstantModel | None = None,
    planner_config: PlannerConfig | None = None,
) -> LabRelayService:
    model = model or ConstantModel()
    return LabRelayService(
        repository=repository if repository is not None else InMemoryRepository(),
        engine=engine if engine is not None else InMemorySearchEngine(model=model),
        context=context if context is not None else RecordingContext(),
        model=model,
        planner_config=planner_config,
    )


async def seed(
    repository: InMemoryRepository, *tasks: LearningTask, plan: LearningPlan | None = None
) -> Project:
    """Persist a project whose active plan owns ``tasks``, as Task 10 will."""
    plan = plan or make_plan(*tasks)
    project = make_project(tasks[0].project_id, active_plan_id=plan.id)
    await repository.save_project(project)
    await repository.save_plan(plan)
    return project


# --- the plan's Task 5 test ----------------------------------------------------


def test_create_project_persists_and_creates_scopes() -> None:
    repository = InMemoryRepository()
    engine = InMemorySearchEngine(model=ConstantModel())
    service = LabRelayService(
        repository=repository,
        engine=engine,
        context=RecordingContext(),
        model=ConstantModel(),
    )
    project = asyncio.run(service.create_project("Aurora", "Deep Learning", "Reproduce experiment"))
    assert isinstance(project, Project)
    assert project.project_scope_id.startswith("project:")
    assert asyncio.run(repository.get_project(project.id)) == project


# --- repository / protocol conformance -----------------------------------------


def _protocol_methods(protocol: type[Protocol]) -> set[str]:
    return {
        name
        for base in protocol.__mro__
        for name, member in vars(base).items()
        if not name.startswith("_") and callable(member)
    }


def test_repository_implements_every_protocol_method() -> None:
    """Task 7 swaps in a SQL repository, so the in-memory one must match the port exactly."""
    names = _protocol_methods(Repository)
    assert names, "the Repository protocol declares no methods"
    for name in names:
        method = getattr(InMemoryRepository, name, None)
        assert method is not None, f"InMemoryRepository is missing {name}"
        assert inspect.iscoroutinefunction(method), f"{name} must be awaitable"
        assert list(inspect.signature(method).parameters) == list(
            inspect.signature(getattr(Repository, name)).parameters
        ), f"{name} drifted from the port signature"


async def test_initialize_is_a_no_op_that_can_be_repeated() -> None:
    repository = InMemoryRepository()

    assert await repository.initialize() is None
    assert await repository.initialize() is None


async def test_unknown_lookups_return_none_rather_than_raising() -> None:
    repository = InMemoryRepository()

    assert await repository.get_project("project-none") is None
    assert await repository.get_asset("asset-none") is None
    assert await repository.get_plan("plan-none") is None
    assert await repository.get_task("task-none") is None
    assert await repository.get_assessment("assessment-none") is None
    assert await repository.list_assets("project-none") == []
    assert await repository.list_events("project-none") == []


async def test_asset_reads_are_scoped_to_their_project() -> None:
    repository = InMemoryRepository()
    await repository.save_project(make_project("project-1"))
    await repository.save_project(make_project("project-2"))
    await repository.save_asset(make_asset("asset-a", "project-1"))
    await repository.save_asset(make_asset("asset-b", "project-2"))

    assert [asset.id for asset in await repository.list_assets("project-1")] == ["asset-a"]
    assert (await repository.get_asset("asset-b")).project_id == "project-2"


async def test_repository_rows_are_not_shared_with_callers() -> None:
    """The store owns its state, exactly like the SQL repository that replaces it.

    Task 10/11 keep holding the plan and task objects they saved while the planner and the
    progress API mutate status, so a returned object must never be a window into the store.
    """
    repository = InMemoryRepository()
    project = make_project()
    plan = make_plan(make_task("task-1", status="todo"))
    await repository.save_project(project)
    await repository.save_plan(plan)
    await repository.save_assessment("assessment-1", "project-1", {"score": 3})
    service = make_service(repository=repository)
    await service.update_task_status("task-1", "in_progress")

    project.title = "mutated by the caller"
    plan.tasks[0].status = "done"
    stored_assessment = await repository.get_assessment("assessment-1")
    stored_assessment["score"] = 99
    stored_event = (await repository.list_events("project-1"))[0]
    stored_event.payload["to"] = "skipped"

    assert (await repository.get_project(project.id)).title == "Aurora"
    assert (await repository.get_task("task-1")).status == "in_progress"
    # The saved plan tracked the transition, and the caller's later mutation never reached it.
    assert (await repository.get_plan("plan-1")).tasks[0].status == "in_progress"
    assert (await repository.get_assessment("assessment-1"))["score"] == 3
    assert (await repository.list_events("project-1"))[0].payload["to"] == "in_progress"


async def test_plan_saves_index_tasks_and_exposes_the_active_plan_and_next_version() -> None:
    repository = InMemoryRepository()
    project = await seed(repository, make_task("task-1"), make_task("task-2", status="locked"))
    stored = await repository.get_plan("plan-1")

    assert [task.id for task in stored.tasks] == ["task-1", "task-2"]
    assert (await repository.get_task("task-2")).status == "locked"
    assert [t.id for t in (await repository.get_active_plan(project.id)).tasks] == [
        "task-1",
        "task-2",
    ]
    assert await repository.get_next_plan_version(project.id) == 2

    # A task update reaches the plan the UI renders, not only the task table.
    await repository.save_task(make_task("task-2", status="todo"))
    assert [t.status for t in (await repository.get_active_plan(project.id)).tasks] == [
        "todo",
        "todo",
    ]


async def test_active_plan_is_absent_until_a_project_points_at_one() -> None:
    repository = InMemoryRepository()
    await repository.save_project(make_project("project-1"))

    assert await repository.get_active_plan("project-1") is None
    assert await repository.get_active_plan("project-unknown") is None
    assert await repository.get_next_plan_version("project-unknown") == 1


async def test_next_plan_version_counts_only_that_project() -> None:
    """Replanning (Task 11) takes this number, so another project must not inflate it."""
    repository = InMemoryRepository()
    other = make_task("task-2", project_id="project-2", plan_id="plan-2")
    await repository.save_project(make_project("project-1", active_plan_id="plan-1"))
    await repository.save_project(make_project("project-2", active_plan_id="plan-2"))
    await repository.save_plan(make_plan(make_task("task-1")))
    await repository.save_plan(make_plan(other, plan_id="plan-2"))

    assert await repository.get_next_plan_version("project-1") == 2
    assert await repository.get_next_plan_version("project-2") == 2

    await repository.save_plan(make_plan(make_task("task-3"), plan_id="plan-3"))

    assert await repository.get_next_plan_version("project-1") == 3
    assert await repository.get_next_plan_version("project-2") == 2

    # Re-saving a plan the importer or a retry already wrote does not mint a version.
    await repository.save_plan(make_plan(make_task("task-1"), plan_id="plan-1"))

    assert await repository.get_next_plan_version("project-1") == 3


async def test_assessments_round_trip_with_their_project() -> None:
    repository = InMemoryRepository()

    await repository.save_assessment("assessment-1", "project-1", {"score": 3})

    assert await repository.get_assessment("assessment-1") == {
        "id": "assessment-1",
        "project_id": "project-1",
        "score": 3,
    }
    assert await repository.get_assessment("assessment-unknown") is None


async def test_save_assessment_never_lets_the_payload_restate_its_identity() -> None:
    """Task 10 files model JSON here; the caller decides which row and project it is.

    An assessment body that carries ``id``/``project_id`` must not be able to rehome itself
    into another project's row — the same rule the task event enforces on ``from``/``to``.
    """
    repository = InMemoryRepository()

    await repository.save_assessment(
        "assessment-1",
        "project-1",
        {"id": "assessment-999", "project_id": "project-2", "score": 3},
    )

    assert await repository.get_assessment("assessment-1") == {
        "id": "assessment-1",
        "project_id": "project-1",
        "score": 3,
    }
    assert list(repository.assessments) == ["assessment-1"]
    assert await repository.get_assessment("assessment-999") is None

    await repository.save_assessment("assessment-2", "project-3", {"id": "assessment-1"})

    stored = await repository.get_assessment("assessment-2")
    assert (stored["id"], stored["project_id"]) == ("assessment-2", "project-3")
    assert (await repository.get_assessment("assessment-1"))["project_id"] == "project-1"


async def test_assessment_rows_are_deep_copied_on_the_way_in_and_out() -> None:
    """A nested answer must not stay a window into the store, in either direction."""
    repository = InMemoryRepository()
    payload: dict[str, object] = {"score": 3, "answers": {"q1": ["weak"]}}

    await repository.save_assessment("assessment-1", "project-1", payload)
    payload["score"] = 99
    payload["answers"]["q1"].append("caller mutation")
    payload["answers"]["q2"] = ["new"]

    assert await repository.get_assessment("assessment-1") == {
        "id": "assessment-1",
        "project_id": "project-1",
        "score": 3,
        "answers": {"q1": ["weak"]},
    }

    stored = await repository.get_assessment("assessment-1")
    stored["answers"]["q1"].append("reader mutation")
    stored["answers"]["q3"] = ["invented"]

    assert (await repository.get_assessment("assessment-1"))["answers"] == {"q1": ["weak"]}
    # The store never writes back through the reference the caller handed it either.
    assert payload["answers"] == {"q1": ["weak", "caller mutation"], "q2": ["new"]}


async def test_events_are_append_only_and_scoped_to_one_project() -> None:
    repository = InMemoryRepository()
    service = make_service(repository=repository)
    await seed(repository, make_task("task-1"))
    second = make_task("task-2", project_id="project-2", plan_id="plan-2")
    await seed(repository, second, plan=make_plan(second, plan_id="plan-2"))

    await service.update_task_status("task-1", "in_progress")
    await service.update_task_status("task-2", "in_progress")
    await service.update_task_status("task-1", "blocked")

    first = await repository.list_events("project-1")
    assert [event.task_id for event in first] == ["task-1", "task-1"]
    assert [event.payload["to"] for event in first] == ["in_progress", "blocked"]
    assert {event.project_id for event in first} == {"project-1"}
    assert len(await repository.list_events("project-2")) == 1


# --- create_project ------------------------------------------------------------


async def test_create_project_writes_the_initial_decision_to_the_project_scope() -> None:
    context = RecordingContext()
    service = make_service(context=context)

    project = await service.create_project("Aurora", "Deep Learning", "Reproduce experiment")

    assert project.id.startswith("project-")
    assert project.learner_scope_id == f"learner:{project.id}"
    # ensure_scopes runs before the ids exist, so the gateway sees the placeholder project.
    assert context.scoped_projects[0].project_scope_id == ""
    assert context.remembered == [
        (
            project.project_scope_id,
            "decision",
            f"项目《{project.title}》（Deep Learning）已创建，目标：Reproduce experiment。"
            "开始建立可追溯交接资料空间。",
            "project initialization",
        )
    ]


async def test_create_project_returns_a_persisted_project_with_a_unique_id() -> None:
    repository = InMemoryRepository()
    service = make_service(repository=repository)

    first = await service.create_project("Aurora", "DL", "one")
    second = await service.create_project("Aurora", "DL", "one")

    assert first.id != second.id
    assert await repository.get_project(first.id) == first
    assert await repository.list_assets(first.id) == []


async def test_create_project_survives_a_memory_write_failure(caplog) -> None:
    """The project record is the source of truth; a down memory service cannot un-make it."""
    repository = InMemoryRepository()
    service = make_service(repository=repository, context=ExplodingContext())

    with caplog.at_level(logging.WARNING, logger=SERVICE_LOGGER):
        project = await service.create_project("Aurora", "DL", "one")

    assert await repository.get_project(project.id) == project
    assert "memory sync failed" in caplog.text


@pytest.mark.parametrize(
    "scopes",
    [
        ("", ""),
        ("project:scope", ""),
        ("", "learner:scope"),
        (None, None),
        (None, "learner:scope"),
        ("project:scope", None),
    ],
)
async def test_create_project_refuses_to_persist_without_two_usable_scopes(
    scopes: tuple[object, object],
) -> None:
    """No scopes means no memory identity, so the project is never written and never remembered."""
    repository = InMemoryRepository()
    context = BadScopeContext(scopes)
    service = make_service(repository=repository, context=context)

    with pytest.raises(ValueError, match="two non-empty memory scope ids"):
        await service.create_project("Aurora", "DL", "one")

    assert repository.projects == {}
    assert context.remembered == []


# --- import_project_files ------------------------------------------------------


async def test_import_project_files_requires_a_known_project(tmp_path: Path) -> None:
    service = make_service()

    with pytest.raises(KeyError):
        await service.import_project_files("project-unknown", tmp_path)


async def test_import_project_files_keeps_the_restricted_observability(tmp_path: Path) -> None:
    """Task 6 renders this dict, so the importer's restricted fields pass through intact."""
    (tmp_path / "baseline.md").write_text(
        "# baseline\nrun train.py and compare metrics\n", encoding="utf-8"
    )
    (tmp_path / "credentials.yaml").write_text("api_key: super-secret-value\n", encoding="utf-8")
    repository = InMemoryRepository()
    service = make_service(repository=repository)
    project = await service.create_project("Aurora", "DL", "baseline")

    result = await service.import_project_files(project.id, tmp_path)

    assert set(result) == {
        "assets",
        "chunks",
        "failed",
        "skipped",
        "restricted",
        "restricted_reasons",
    }
    assert result["assets"] == 2
    assert result["restricted"] == 1 == len(result["restricted_reasons"])
    assert list(result["restricted_reasons"].values()) == [["api_secret"]]
    # Pattern names travel, file content never does (spec 19.1).
    assert "super-secret-value" not in str(result)

    restricted = next(
        asset for asset in await repository.list_assets(project.id) if asset.status == "restricted"
    )
    assert restricted.kind == "config"
    assert restricted.id in result["restricted_reasons"]

    hits = await service.search(project.id, "baseline metrics", [])
    assert hits and {hit.citation.title for hit in hits} == {"baseline.md"}


# --- search --------------------------------------------------------------------


async def test_search_is_scoped_and_kind_filtered_by_the_engine(tmp_path: Path) -> None:
    repository = InMemoryRepository()
    service = make_service(repository=repository)
    first = await service.create_project("A", "DL", "one")
    second = await service.create_project("B", "DL", "two")
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "train.py").write_text("def run_baseline():\n    return 1\n", encoding="utf-8")
    (root_b / "notes.md").write_text("baseline notes for project b\n", encoding="utf-8")
    await service.import_project_files(first.id, root_a)
    await service.import_project_files(second.id, root_b)

    hits = await service.search(first.id, "baseline", [])
    owned = {asset.id for asset in await repository.list_assets(first.id)}
    assert hits and owned
    assert all(isinstance(hit, SearchHit) for hit in hits)
    assert {hit.citation.asset_id for hit in hits} <= owned
    # The other project's evidence never leaks, and a kind filter narrows this to nothing.
    assert await service.search(first.id, "baseline", ["note"]) == []
    assert len(await service.search(first.id, "baseline", [], limit=1)) == 1


def test_search_defaults_to_the_configured_output_limit() -> None:
    config = yaml.safe_load(RETRIEVAL_CONFIG.read_text(encoding="utf-8"))
    output_limit = config["retrieval"]["fusion"]["output_limit"]

    assert inspect.signature(LabRelayService.search).parameters["limit"].default == output_limit


# --- planner configuration and wiring ------------------------------------------


def test_service_defaults_to_the_shipped_planner_constraints() -> None:
    constraints = yaml.safe_load(PLANNER_CONFIG.read_text(encoding="utf-8"))["planning"][
        "constraints"
    ]

    service = make_service()

    assert isinstance(service.planner_config, PlannerConfig)
    assert (
        service.planner_config.environment_before_experiments
        == constraints["environment_before_experiments"]
    )
    assert (
        service.planner_config.every_task_requires_citation
        == constraints["every_task_requires_citation"]
    )


def test_service_keeps_the_injected_planner_config() -> None:
    planner_config = PlannerConfig(every_task_requires_citation=False)

    assert make_service(planner_config=planner_config).planner_config is planner_config


def test_service_exposes_the_collaborators_later_tasks_reach_through() -> None:
    """Task 6 reads ``service.repository``; the engine is ``engine``, never ``search``."""
    repository = InMemoryRepository()
    engine = InMemorySearchEngine(model=ConstantModel())
    context = RecordingContext()
    model = ConstantModel()

    service = LabRelayService(repository=repository, engine=engine, context=context, model=model)

    assert service.repository is repository
    assert service.engine is engine
    assert service.context is context
    assert service.model is model


# --- update_task_status --------------------------------------------------------


async def test_valid_transition_saves_the_task_and_appends_one_event() -> None:
    repository = InMemoryRepository()
    context = RecordingContext()
    service = make_service(repository=repository, context=context)
    project = await seed(repository, make_task("task-1", status="todo"))

    updated = await service.update_task_status("task-1", "in_progress", {"note": "GPU ready"})

    assert updated.status == "in_progress"
    assert (await repository.get_task("task-1")).status == "in_progress"
    events = await repository.list_events(project.id)
    assert len(events) == 1
    event = events[0]
    assert event.id.startswith("event-")
    assert (event.project_id, event.plan_id, event.task_id) == (project.id, "plan-1", "task-1")
    assert event.event_type == "status_changed"
    assert event.payload == {"from": "todo", "to": "in_progress", "note": "GPU ready"}
    # Task 11 turns this stamp into the cross-session progress summary.
    assert event.occurred_at.endswith("+00:00")
    assert context.remembered == []


async def test_the_event_records_the_real_transition_not_the_payload_claim() -> None:
    """A caller payload must not rewrite history: ``from``/``to`` belong to the service."""
    repository = InMemoryRepository()
    service = make_service(repository=repository)
    await seed(repository, make_task("task-1", status="todo"))

    await service.update_task_status("task-1", "in_progress", {"from": "done", "to": "skipped"})

    assert (await repository.list_events("project-1"))[0].payload == {
        "from": "todo",
        "to": "in_progress",
    }


@pytest.mark.parametrize(
    ("start", "target"),
    [
        ("todo", "done"),
        ("todo", "todo"),
        ("locked", "in_progress"),
        ("in_progress", "todo"),
        ("done", "in_progress"),
        ("done", "todo"),
        ("skipped", "todo"),
    ],
)
async def test_a_transition_the_domain_table_forbids_is_rejected(start: str, target: str) -> None:
    repository = InMemoryRepository()
    service = make_service(repository=repository)
    await seed(repository, make_task("task-1", status=start))

    with pytest.raises(ValueError, match="Cannot transition"):
        await service.update_task_status("task-1", target)

    assert (await repository.get_task("task-1")).status == start
    assert await repository.list_events("project-1") == []


async def test_every_transition_the_table_allows_is_accepted() -> None:
    """The service must not be stricter than the domain rule it delegates to."""
    for start, targets in ALLOWED_TASK_TRANSITIONS.items():
        for target in sorted(targets):
            repository = InMemoryRepository()
            service = make_service(repository=repository)
            await seed(repository, make_task("task-1", status=start))

            updated = await service.update_task_status("task-1", target)

            assert updated.status == target


async def test_an_unknown_status_is_rejected_for_a_live_task() -> None:
    repository = InMemoryRepository()
    service = make_service(repository=repository)
    await seed(repository, make_task("task-1", status="todo"))

    with pytest.raises(ValueError, match="Cannot transition"):
        await service.update_task_status("task-1", "archived")


async def test_an_unknown_task_raises_key_error_without_touching_memory() -> None:
    context = RecordingContext()
    service = make_service(context=context)

    with pytest.raises(KeyError):
        await service.update_task_status("task-unknown", "done")

    assert context.remembered == []


async def test_completing_a_task_writes_learner_memory_through_the_gateway() -> None:
    repository = InMemoryRepository()
    context = RecordingContext()
    service = make_service(repository=repository, context=context)
    project = await seed(repository, make_task("task-1", status="in_progress"))

    done = await service.update_task_status("task-1", "done", {"summary": "已复述规则"})

    assert done.status == "done"
    assert context.remembered == [
        (
            project.learner_scope_id,
            "progress",
            f"已完成任务「{done.title}」。下一阶段重点：查看下一项可执行任务。",
            "task:task-1",
        )
    ]
    assert (await repository.list_events(project.id))[0].payload["summary"] == "已复述规则"
    # A memory write that succeeded leaves no failure record behind.
    assert [e.event_type for e in await repository.list_events(project.id)] == ["status_changed"]


async def test_a_blocked_task_is_recorded_without_touching_learner_memory() -> None:
    context = RecordingContext()
    repository = InMemoryRepository()
    service = make_service(repository=repository, context=context)
    await seed(repository, make_task("task-1", status="in_progress"))

    await service.update_task_status("task-1", "blocked", {"reason": "缺 GPU"})

    assert context.remembered == []
    assert (await repository.list_events("project-1"))[0].payload == {
        "from": "in_progress",
        "to": "blocked",
        "reason": "缺 GPU",
    }


async def test_memory_failure_keeps_the_progress_and_the_event(caplog) -> None:
    """Spec 20: a failed memory write must never lose a saved transition.

    The transition and its ``status_changed`` event stay exactly as they were; the failure is
    appended as its own ``memory_sync_failed`` event, because a warning line alone is not a
    machine-readable record Task 6/14 can render 「进度已保存但跨会话记忆未同步」 from.
    """
    repository = InMemoryRepository()
    service = make_service(repository=repository, context=ExplodingContext())
    await seed(repository, make_task("task-1", status="in_progress"))

    with caplog.at_level(logging.WARNING, logger=SERVICE_LOGGER):
        done = await service.update_task_status("task-1", "done", {"summary": "完成"})

    events = await repository.list_events("project-1")
    assert done.status == "done"
    assert (await repository.get_task("task-1")).status == "done"
    assert [event.event_type for event in events] == ["status_changed", "memory_sync_failed"]
    # The successful transition event is untouched by the failure that followed it.
    assert events[0].payload == {"from": "in_progress", "to": "done", "summary": "完成"}
    assert "memory sync failed" in caplog.text
    assert "learner:project-1" in caplog.text
    # Spec 19.1: a gateway error message never travels into the log or into the event trail.
    assert "super-secret-value" not in caplog.text
    assert "super-secret-value" not in str(events)


async def test_the_memory_sync_failure_event_names_only_the_task_and_the_error_class() -> None:
    """The failure record is append-only and carries identifiers, never memory content."""
    repository = InMemoryRepository()
    service = make_service(repository=repository, context=ExplodingContext())
    project = await seed(repository, make_task("task-1", status="in_progress"))

    await service.update_task_status("task-1", "done")

    status_changed, failure = await repository.list_events(project.id)
    assert failure.id.startswith("event-") and failure.id != status_changed.id
    assert (failure.project_id, failure.plan_id, failure.task_id) == (
        project.id,
        "plan-1",
        "task-1",
    )
    assert failure.event_type == "memory_sync_failed"
    assert failure.payload == {"task_id": "task-1", "exception_class": "RuntimeError"}
    assert failure.occurred_at.endswith("+00:00")


async def test_a_transition_that_needs_no_memory_writes_no_failure_event() -> None:
    repository = InMemoryRepository()
    service = make_service(repository=repository, context=ExplodingContext())
    await seed(repository, make_task("task-1", status="todo"))

    await service.update_task_status("task-1", "in_progress")

    assert [e.event_type for e in await repository.list_events("project-1")] == ["status_changed"]


async def test_completing_a_task_of_a_project_that_was_never_saved_stays_quiet() -> None:
    repository = InMemoryRepository()
    context = RecordingContext()
    service = make_service(repository=repository, context=context)
    await repository.save_plan(make_plan(make_task("task-1", status="in_progress")))

    done = await service.update_task_status("task-1", "done")

    assert done.status == "done"
    assert context.remembered == []
    # A task with no project record attempts no memory write at all, so it reports no failure.
    assert [e.event_type for e in await repository.list_events("project-1")] == ["status_changed"]
