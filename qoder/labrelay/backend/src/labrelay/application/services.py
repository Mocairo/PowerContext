"""Application-layer orchestration: the LabRelay use cases Tasks 6 and 12 wire up.

:class:`LabRelayService` is the only place a use case crosses a port boundary, which keeps
the REST layer (Task 6) thin and lets the seekdb repository, the SQL engine and the
PowerContext adapter (Tasks 7 and 9) be swapped without touching this file. It holds no
domain rules of its own: task status legality comes from
:data:`labrelay.domain.models.ALLOWED_TASK_TRANSITIONS` and evidence handling comes from the
Task 3 importer, so the service sequences steps and nothing else decides them.

Memory writes are the one deliberate degradation path here. Spec 20 requires a task
completion to stay saved when the memory service is down, so a ``remember`` failure is
logged and swallowed instead of failing the transition; scope creation (``ensure_scopes``)
is *not* degraded, because without its two scopes a project has no memory identity at all.
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
        """Open a project, give it its two memory scopes and remember why it exists."""
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
        project = project.model_copy(
            update={"project_scope_id": project_scope, "learner_scope_id": learner_scope}
        )
        await self.repository.save_project(project)
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
        """Apply one status transition, append its TaskEvent, sync memory when it completes."""
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
                event_type="status_changed",
                # ``from``/``to`` are written last so a caller payload can enrich the event
                # but never rewrite which transition actually happened (spec 15, 16.1).
                payload={**(payload or {}), "from": task.status, "to": status},
                occurred_at=datetime.now(UTC).isoformat(),
            )
        )
        if status == "done":
            await self._sync_progress_memory(updated)
        return updated

    async def _sync_progress_memory(self, task: LearningTask) -> None:
        """Hand the finished task to learner memory so a new session can recover progress."""
        project = await self.repository.get_project(task.project_id)
        if project is None:
            return
        await self._remember(
            project.learner_scope_id,
            PROGRESS_KIND,
            f"已完成任务「{task.title}」。下一阶段重点：查看下一项可执行任务。",
            reason=f"task:{task.id}",
            project_id=project.id,
        )

    async def _remember(
        self,
        scope_id: str,
        kind: str,
        text: str,
        *,
        reason: str,
        project_id: str,
    ) -> None:
        """Write one memory note, degrading to a warning if the memory service is down.

        Only the exception type is logged: a gateway failure can echo a request URL or an
        Authorization header, and spec 19.1 keeps credentials out of the logs entirely.
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
