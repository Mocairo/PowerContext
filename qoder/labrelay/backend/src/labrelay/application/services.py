"""Application-layer orchestration: the LabRelay use cases Tasks 6 and 12 wire up.

:class:`LabRelayService` is the only place a use case crosses a port boundary, which keeps
the REST layer (Task 6) thin and lets the seekdb repository, the SQL engine and the
PowerContext adapter (Tasks 7 and 9) be swapped without touching this file. It holds no
domain rules of its own: task status legality comes from
:data:`labrelay.domain.models.ALLOWED_TASK_TRANSITIONS` and evidence handling comes from the
Task 3 importer, so the service sequences steps and nothing else decides them.

Memory writes are the one deliberate degradation path here. Spec 20 requires a task
completion to stay saved when the memory service is down, so a ``remember`` failure is
logged *and* appended as a ``memory_sync_failed`` TaskEvent rather than failing the
transition: Task 6/14 need a machine-readable record to show 「记忆未同步」, and a warning line
is not one. Scope creation (``ensure_scopes``) is *not* degraded, because without its two
scope ids a project has no memory identity at all — so a gateway that hands back empty or
missing ids is refused before the project is ever persisted.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from labrelay.config import PlannerConfig
from labrelay.domain.assessment import Assessment, build_assessment, new_assessment_id
from labrelay.domain.models import (
    ALLOWED_TASK_TRANSITIONS,
    LearningPlan,
    LearningTask,
    Project,
    TaskEvent,
)
from labrelay.domain.ports import ContextGateway, ModelGateway, Repository, SearchEngine, SearchHit
from labrelay.importers.assets import ImportResult, import_directory
from labrelay.learning.progress import (
    STALE_PLAN,
    TERMINAL_STATUSES,
    TaskTransitionRejected,
    apply_unlocks,
    build_learning_context,
    build_reverse_handoff_draft,
    check_progress_rule,
    evidence_by_task,
    read_learner_memory,
    record_evidence,
    require_active_plan,
)
from labrelay.learning.replan import activate_replan, build_replanned_plan, replan_summary
from labrelay.planning.generator import generate_learning_plan

logger = logging.getLogger("labrelay.application.services")

# The memory kind each degradation-prone write is filed under, so the log line and the
# stored note agree on what was being remembered.
DECISION_KIND = "decision"
PROGRESS_KIND = "progress"
PREFERENCE_KIND = "preference"

# The two event types Task 5 appends. "evidence_submitted" and the replanning events arrive
# with Task 11; until the vocabulary is complete these stay plain strings on the TaskEvent.
STATUS_CHANGED_EVENT = "status_changed"
MEMORY_SYNC_FAILED_EVENT = "memory_sync_failed"


class ContextServiceUnavailableError(ValueError):
    """The memory gateway could not give a new project its two scope ids.

    A dependency answer, not bad input: spec 17.3 reserves 503 for exactly this, so the REST
    layer maps *this class* — and never an arbitrary ``ValueError``, which the service also
    raises for an illegal task transition — to that status. It stays a ``ValueError`` subclass
    because Task 5's contract already refuses an unusable gateway answer that way.

    ``project_id`` names the project that was *not* created, so the API can log the identifier
    the operator needs without logging this message, which is never shown to a caller.
    """

    def __init__(self, project_id: str) -> None:
        super().__init__(
            "ContextGateway must return two non-empty memory scope ids; "
            f"project {project_id} was not created"
        )
        self.project_id = project_id


class IllegalTaskTransition(ValueError):
    """The domain transition table refused this status pair; it is not a dependency failure."""


class LabRelayService:
    """Project creation, evidence import, retrieval, assessment, planning and task progress."""

    def __init__(
        self,
        repository: Repository,
        engine: SearchEngine,
        context: ContextGateway,
        model: ModelGateway,
        planner_config: PlannerConfig | None = None,
    ) -> None:
        self.repository = repository
        # ``engine``, never ``search``: the attribute is the retrieval port, while ``search``
        # is the use-case method below and Task 6 reaches through ``service.engine`` for it.
        self.engine = engine
        self.context = context
        self.model = model
        # Task 10's planner enforces these; the service only carries them to the validator.
        self.planner_config = planner_config or PlannerConfig()

    async def create_project(self, title: str, research_area: str, description: str) -> Project:
        """Open a project, give it its two memory scopes and remember why it exists.

        Raises :class:`ContextServiceUnavailableError` when the gateway hands back an empty or
        missing scope id: such a project has no memory identity, so it is refused instead of
        persisted half-made.
        """
        project = Project(
            id=f"project-{uuid.uuid4().hex}",
            title=title,
            research_area=research_area,
            description=description,
            project_scope_id="",
            learner_scope_id="",
        )
        # The scopes come back from the gateway rather than being guessed here: PowerContext
        # owns their identifiers, and a project is only persisted once it has them.
        project_scope, learner_scope = await self.context.ensure_scopes(project)
        # Both ids have to be real before anything is written. An empty scope would be stored
        # as a usable one and every later memory call would silently target nothing, so this
        # fails loudly instead — the only place Task 5 refuses a gateway answer.
        if not project_scope or not learner_scope:
            raise ContextServiceUnavailableError(project.id)
        project = project.model_copy(
            update={"project_scope_id": project_scope, "learner_scope_id": learner_scope}
        )
        await self.repository.save_project(project)
        # A failed decision note cannot undo the project, and there is no task to attach a
        # memory_sync_failed event to: the warning log is the record until Task 12's health
        # check reports PowerContext availability.
        await self._remember(
            project.project_scope_id,
            DECISION_KIND,
            f"项目 {project.title} 已创建，开始建立可追溯交接资料空间。",
            reason="project initialization",
            project_id=project.id,
        )
        return project

    async def import_project_files(self, project_id: str, source_path: Path) -> ImportResult:
        """Import a handoff folder as assets and searchable evidence for one project.

        The importer's result is returned as-is, including ``restricted`` and
        ``restricted_reasons``, because the import route has to tell an operator how much
        evidence was withheld from the index and why.
        """
        project = await self.repository.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        return await import_directory(self.repository, self.engine, project_id, source_path)

    async def search(
        self, project_id: str, query: str, kinds: list[str], limit: int = 12
    ) -> list[SearchHit]:
        """Hybrid retrieval over one project's active evidence; the engine owns the ranking."""
        return await self.engine.search(project_id, query, kinds, limit)

    async def submit_assessment(self, project_id: str, payload: dict) -> Assessment:
        """Score one learner answer set, file it and hand the goal to learner memory.

        The identity is minted here and the project id is taken from the caller that already
        resolved it, so a payload cannot choose which row it writes onto or plan one project out
        of another's answers. Raises :class:`KeyError` for an unknown project; a malformed answer
        set is pydantic's own ``ValidationError`` (a ``ValueError``), which the REST layer already
        turns into a 422 because it validates the same model.
        """
        project = await self.repository.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        assessment = build_assessment(
            {**payload, "id": new_assessment_id(), "project_id": project.id}
        )
        await self.repository.save_assessment(
            assessment.id, project.id, assessment.model_dump(mode="json")
        )
        # Spec 14: the answers are used to shape the path, so what is remembered is the goal and
        # the budget, never a judgement about the learner.
        await self._remember(
            project.learner_scope_id,
            PREFERENCE_KIND,
            f"接手人目标为 {assessment.target}，每周可投入 {assessment.hours_per_week} 小时。",
            reason=f"assessment:{assessment.id}",
            project_id=project.id,
        )
        return assessment

    async def generate_plan(self, project_id: str, assessment_id: str) -> LearningPlan:
        """Generate, validate and possibly activate this project's next plan version.

        Generation itself is :mod:`labrelay.planning.generator`; this wrapper resolves the two
        ids, refuses an assessment that belongs to another project and records what the learner
        should be able to recover in a new session — including why a plan stayed a draft.
        """
        project = await self.repository.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        assessment_payload = await self.repository.get_assessment(assessment_id)
        if assessment_payload is None:
            raise KeyError(assessment_id)
        assessment = Assessment.model_validate(assessment_payload)
        if assessment.project_id != project.id:
            # Not this project's answers. Raised as a missing assessment on purpose: the API must
            # not confirm that the id exists somewhere else (spec 19.1).
            raise KeyError(assessment_id)

        plan = await generate_learning_plan(
            repository=self.repository,
            engine=self.engine,
            context=self.context,
            model=self.model,
            config=self.planner_config,
            project=project,
            assessment=assessment,
        )
        await self._remember(
            project.learner_scope_id,
            PROGRESS_KIND,
            f"已生成学习路径版本 {plan.version}，当前共有 {len(plan.tasks)} 个任务。",
            reason=f"plan:{plan.id}",
            project_id=project.id,
        )
        gap_titles = "、".join(task.title for task in plan.tasks if task.status == "needs_material")
        if gap_titles:
            await self._remember(
                project.learner_scope_id,
                PROGRESS_KIND,
                f"任务「{gap_titles}」资料不足，已保存为待补充草稿，补齐资料前不会开放。",
                reason=f"plan:{plan.id}:needs_material",
                project_id=project.id,
            )
        return plan

    async def update_task_status(
        self, task_id: str, status: str, payload: dict | None = None
    ) -> LearningTask:
        """Apply one status transition, append its TaskEvent, sync memory when it completes.

        Completion appends a second event only if the learner-memory write failed; the
        transition itself always lands first and stays landed.
        """
        task = await self.repository.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if status not in ALLOWED_TASK_TRANSITIONS[task.status]:
            raise IllegalTaskTransition(f"Cannot transition from {task.status} to {status}")

        updated = task.model_copy(update={"status": status})
        await self.repository.save_task(updated)
        await self.repository.append_event(
            TaskEvent(
                id=f"event-{uuid.uuid4().hex}",
                project_id=task.project_id,
                plan_id=task.plan_id,
                task_id=task.id,
                event_type=STATUS_CHANGED_EVENT,
                # ``from``/``to`` are written last so a caller payload can enrich the event
                # but never rewrite which transition actually happened (spec 15, 16.1).
                payload={**(payload or {}), "from": task.status, "to": status},
                occurred_at=datetime.now(UTC).isoformat(),
            )
        )
        if status == "done":
            failure = await self._sync_progress_memory(updated)
            if failure is not None:
                await self._append_memory_sync_failure(updated, failure)
        return updated

    async def update_task(
        self, task_id: str, status: str, evidence: dict[str, Any] | None = None
    ) -> LearningTask:
        """Apply one learner-authored progress write, with the rules the table alone omits.

        :meth:`update_task_status` stays the only writer of a transition, an event and a memory
        sync; this is the door the API comes through, and it refuses the three writes that would
        otherwise be legal but meaningless: a completion with none of the evidence the task
        declared, a skip with no reason and no confirmation behind it, and a locked task opened
        before its chain is finished. A refusal raises before anything is stored, so the task, the
        trail and the memory service are all exactly as they were.

        A finished task then opens its own successors — and only those whose every prerequisite is
        already done or skipped — so a seven-stage path stays walkable one step at a time.
        """
        task = await self.repository.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        project = await self.repository.get_project(task.project_id)
        if project is None or project.active_plan_id != task.plan_id:
            raise TaskTransitionRejected(
                "This task belongs to a plan that is no longer active.",
                reason=STALE_PLAN,
            )
        plan = await self.repository.get_plan(task.plan_id)
        check_progress_rule(task, status, evidence, plan)

        status_meta = {
            key: value
            for key, value in (evidence or {}).items()
            if key in {"reason", "skip_confirmed_by", "assessment_id"}
        }
        updated = await self.update_task_status(task_id, status, status_meta)
        if status == "done":
            await record_evidence(self.repository, updated, evidence or {})
        if status in TERMINAL_STATUSES:
            # Re-read the plan: the transition above rewrote the row this pass reads.
            refreshed = await self.repository.get_plan(updated.plan_id)
            if refreshed is not None:
                await apply_unlocks(self.repository, refreshed, unlocked_by=updated.id)
        return updated

    async def replan_project(self, project_id: str, reason: str) -> LearningPlan:
        """Replace the unfinished part of this project's path with a new immutable version.

        The active plan's tasks and its whole event trail are the only sources: what is finished is
        carried forward untouched into the new version's own id space, what is not is handed to the
        model, and the result goes through the same validator as a first generation. Nothing is
        written until the new version is accepted, so a rejection leaves the project planning
        exactly where it was (spec 16.3, FR-PLN-009).
        """
        project = await self.repository.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        source = await require_active_plan(self.repository, project.id)
        events = await self.repository.list_events(project.id)
        plan = await build_replanned_plan(
            repository=self.repository,
            engine=self.engine,
            model=self.model,
            config=self.planner_config,
            project=project,
            source=source,
            reason=reason,
            evidence=evidence_by_task(events),
            events=events,
        )
        activated = await activate_replan(self.repository, plan, project)
        await self._remember(
            project.learner_scope_id,
            PROGRESS_KIND,
            replan_summary(activated),
            reason=f"replan:{activated.id}",
            project_id=project.id,
        )
        return activated

    async def learning_context(self, project_id: str) -> dict[str, Any]:
        """Answer 「where was I?」 for a new session: stage, next task, progress, blockers.

        The facts come from the repository; the Learner Scope only decides how they are phrased,
        and :func:`~labrelay.learning.progress.read_learner_memory` reports which of the two it
        got, so the UI can say 「记忆未同步」 instead of implying the learner never got that
        far (FR-PRG-005).
        """
        project = await self.repository.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        plan = await require_active_plan(self.repository, project.id)
        events = await self.repository.list_events(project.id)
        memory = await read_learner_memory(self.context, project)
        return build_learning_context(project, plan, events, memory)

    async def reverse_handoff_draft(self, project_id: str) -> dict[str, Any]:
        """Draft the four-field reverse Handoff out of the durable progress record.

        Proposal only: committing it stays Task 9's job, so the learner confirms a draft that says
        what the events say rather than one the model re-told.
        """
        project = await self.repository.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        plan = await require_active_plan(self.repository, project.id)
        events = await self.repository.list_events(project.id)
        return build_reverse_handoff_draft(project, plan, events)

    async def _sync_progress_memory(self, task: LearningTask) -> str | None:
        """Hand the finished task to learner memory so a new session can recover progress.

        Returns the exception class name when the write failed, else ``None``. A task whose
        project was never saved is not a memory failure — no write was attempted — so it also
        returns ``None``.
        """
        project = await self.repository.get_project(task.project_id)
        if project is None:
            return None
        return await self._remember(
            project.learner_scope_id,
            PROGRESS_KIND,
            f"已完成任务「{task.title}」。下一阶段重点：查看下一项可执行任务。",
            reason=f"task:{task.id}",
            project_id=project.id,
        )

    async def _append_memory_sync_failure(self, task: LearningTask, exception_class: str) -> None:
        """Record a failed progress-memory write as its own append-only event (spec 20).

        The payload carries the task id and the exception class name and nothing else: a
        gateway error message can echo a request URL or an ``Authorization`` header, and
        spec 19.1 keeps that out of both logs and stored state. The ``status_changed`` event
        already written is never rewritten, because TaskEvents are append-only.
        """
        await self.repository.append_event(
            TaskEvent(
                id=f"event-{uuid.uuid4().hex}",
                project_id=task.project_id,
                plan_id=task.plan_id,
                task_id=task.id,
                event_type=MEMORY_SYNC_FAILED_EVENT,
                payload={"task_id": task.id, "exception_class": exception_class},
                occurred_at=datetime.now(UTC).isoformat(),
            )
        )

    async def _remember(
        self,
        scope_id: str,
        kind: str,
        text: str,
        *,
        reason: str,
        project_id: str,
    ) -> str | None:
        """Write one memory note, degrading to a warning if the memory service is down.

        Returns the exception class name on failure and ``None`` on success, so the caller
        can decide whether the failure is worth a durable record; the use case itself always
        finishes. Only the exception type is logged: a gateway failure can echo a request
        URL or an Authorization header, and spec 19.1 keeps credentials out of the logs.
        """
        try:
            await self.context.remember(scope_id, kind, text, reason=reason)
        except Exception as exc:
            logger.warning(
                "memory sync failed operation=remember project_id=%s scope_id=%s kind=%s "
                "reason=remember_failed detail=%s",
                project_id,
                scope_id,
                kind,
                type(exc).__name__,
            )
            return type(exc).__name__
        return None
