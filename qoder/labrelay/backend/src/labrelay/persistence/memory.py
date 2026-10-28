"""In-memory ``Repository``: the dict-backed store that carries LabRelay's MVP state.

This is the implementation the tests, the demo run and Task 6 run against until Task 7
replaces it with the seekdb repository. It satisfies :class:`labrelay.domain.ports.Repository`
method for method — including ``initialize``, the assessment store, the active-plan
pointer, the next plan version and project-scoped events — so the two implementations are
interchangeable behind the port and no caller has to learn which store it got.

State is copied on the way in and on the way out: the repository owns its rows. A planner
that keeps holding the ``LearningPlan`` it saved must not be able to rewrite stored state
by mutating that object, and a reader must not be able to persist a change without calling
``save_*`` — which is exactly the behaviour the SQL repository will have. The copy is deep, so
a nested value inside a plain-dict row (an assessment answer) is not a live window into the
store in either direction.
"""

from __future__ import annotations

from copy import deepcopy

from labrelay.domain.models import Asset, LearningPlan, LearningTask, Project, TaskEvent


class InMemoryRepository:
    """Project-scoped storage for projects, assets, assessments, plans, tasks and events."""

    def __init__(self) -> None:
        self.projects: dict[str, Project] = {}
        self.assets: dict[str, Asset] = {}
        self.plans: dict[str, LearningPlan] = {}
        self.tasks: dict[str, LearningTask] = {}
        self.events: list[TaskEvent] = []
        self.assessments: dict[str, dict[str, object]] = {}

    async def initialize(self) -> None:
        """No schema to create; repeatable so startup wiring can call it unconditionally."""
        return None

    async def save_project(self, project: Project) -> None:
        self.projects[project.id] = project.model_copy(deep=True)

    async def get_project(self, project_id: str) -> Project | None:
        project = self.projects.get(project_id)
        return project.model_copy(deep=True) if project is not None else None

    async def save_asset(self, asset: Asset) -> None:
        self.assets[asset.id] = asset.model_copy(deep=True)

    async def get_asset(self, asset_id: str) -> Asset | None:
        asset = self.assets.get(asset_id)
        return asset.model_copy(deep=True) if asset is not None else None

    async def list_assets(self, project_id: str) -> list[Asset]:
        """One project's assets in import order, and never another project's."""
        return [
            asset.model_copy(deep=True)
            for asset in self.assets.values()
            if asset.project_id == project_id
        ]

    async def save_assessment(
        self, assessment_id: str, project_id: str, payload: dict[str, object]
    ) -> None:
        """File one assessment under the identity the caller named, deep-copied.

        ``id``/``project_id`` are written last for the same reason the task event records the
        real transition: the payload is model output (Task 10) and must not rehome itself into
        another project's row by carrying those keys. The deep copy keeps the caller's nested
        answers out of the store — Task 10 keeps the assessment JSON alive after filing it.
        """
        self.assessments[assessment_id] = deepcopy(
            {**payload, "id": assessment_id, "project_id": project_id}
        )

    async def get_assessment(self, assessment_id: str) -> dict[str, object] | None:
        """A detached copy, so a reader cannot edit a stored answer without saving it back."""
        stored = self.assessments.get(assessment_id)
        return deepcopy(stored) if stored is not None else None

    async def save_plan(self, plan: LearningPlan) -> None:
        """Store the plan and index its tasks, so a plan id is never a dangling reference."""
        stored = plan.model_copy(deep=True)
        self.plans[stored.id] = stored
        for task in stored.tasks:
            self.tasks[task.id] = task.model_copy(deep=True)

    async def get_plan(self, plan_id: str) -> LearningPlan | None:
        plan = self.plans.get(plan_id)
        return plan.model_copy(deep=True) if plan is not None else None

    async def save_task(self, task: LearningTask) -> None:
        """Update a task in place and keep the plan that carries it in step with it.

        Tasks are stored individually because the progress API reads one by id, but the plan
        object is what the dashboard and the planner render, so a status written here has to
        be visible through :meth:`get_active_plan` on the next read.
        """
        stored = task.model_copy(deep=True)
        self.tasks[stored.id] = stored
        plan = self.plans.get(stored.plan_id)
        if plan is not None:
            plan.tasks = [
                stored.model_copy(deep=True) if item.id == stored.id else item
                for item in plan.tasks
            ]

    async def get_task(self, task_id: str) -> LearningTask | None:
        task = self.tasks.get(task_id)
        return task.model_copy(deep=True) if task is not None else None

    async def append_event(self, event: TaskEvent) -> None:
        """TaskEvents are append-only (spec 11.1): nothing here rewrites or deletes one."""
        self.events.append(event.model_copy(deep=True))

    async def list_events(self, project_id: str) -> list[TaskEvent]:
        """One project's events in the order they were appended."""
        return [
            event.model_copy(deep=True) for event in self.events if event.project_id == project_id
        ]

    async def get_active_plan(self, project_id: str) -> LearningPlan | None:
        """The plan the project points at, or ``None`` before the first plan is activated."""
        project = self.projects.get(project_id)
        if project is None or project.active_plan_id is None:
            return None
        plan = self.plans.get(project.active_plan_id)
        return plan.model_copy(deep=True) if plan is not None else None

    async def get_next_plan_version(self, project_id: str) -> int:
        """Versions grow by project and never repeat, so replanning preserves history.

        Superseded plans still count: spec 16.3 keeps them, and a new version number has to
        be free of them rather than reused.
        """
        return 1 + sum(1 for plan in self.plans.values() if plan.project_id == project_id)
