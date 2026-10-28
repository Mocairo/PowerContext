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

from labrelay.config import PlannerConfig
from labrelay.domain.models import (
    ALLOWED_TASK_TRANSITIONS,
    LearningTask,
    Project,
    TaskEvent,
)
from labrelay.domain.ports import ContextGateway, ModelGateway, Repository, SearchEngine, SearchHit
from labrelay.importers.assets import ImportResult, import_directory

logger = logging.getLogger("labrelay.application.services")

# The memory kind each degradation-prone write is filed under, so the log line and the
# stored note agree on what was being remembered.
DECISION_KIND = "decision"
PROGRESS_KIND = "progress"

# The two event types Task 5 appends. "evidence_submitted" and the replanning events arrive
# with Task 11; until the vocabulary is complete these stay plain strings on the TaskEvent.
STATUS_CHANGED_EVENT = "status_changed"
MEMORY_SYNC_FAILED_EVENT = "memory_sync_failed"


class LabRelayService:
    """Project creation, evidence import, retrieval and task progress for one project store."""

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
        # Task 10's planner reads these constraints; nothing here enforces them yet.
        self.planner_config = planner_config or PlannerConfig()

    async def create_project(self, title: str, research_area: str, description: str) -> Project:
        """Open a project, give it its two memory scopes and remember why it exists.

        Raises ``ValueError`` when the gateway hands back an empty or missing scope id: such
        a project has no memory identity, so it is refused instead of persisted half-made.
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
            raise ValueError(
                "ContextGateway must return two non-empty memory scope ids; "
                f"project {project.id} was not created"
            )
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
            raise ValueError(f"Cannot transition from {task.status} to {status}")

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
