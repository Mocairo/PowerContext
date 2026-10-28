"""Plan generation: committed Handoff plus retrieved evidence becomes a ``LearningPlan``.

The order below is spec 15.2 and every step earns its place:

1. read the Handoff the leaving researcher confirmed (project scope, not the learner's own notes),
2. ask retrieval for each stage's evidence, so the only ``asset_id`` a model can name is one this
   project actually holds,
3. request strict JSON from the generation gateway,
4. build ``LearningTask`` rows and hand the plan to :mod:`labrelay.planning.validator`,
5. save, and activate **only** when no task still says ``needs_material``.

Two decisions are deliberate. The ``plan_id`` is minted *before* the model call and sent with the
payload, because generated task ids have to be namespaced per plan version: a second plan for the
same assessment must not write onto the first plan's primary keys (the carry-forward from Task 8).
And a gap never raises: an under-evidenced baseline is a real, useful answer, so the plan is kept
as a draft, the reason is logged once with a stable code, and the learner pointer stays where it
was.
"""

from __future__ import annotations

import logging
import uuid

from pydantic import ValidationError

from labrelay.config import PlannerConfig
from labrelay.domain.assessment import Assessment
from labrelay.domain.models import LearningPlan, LearningTask, Project
from labrelay.domain.ports import ContextGateway, ModelGateway, Repository, SearchEngine
from labrelay.planning.stages import (
    PLANNING_STAGES,
    STAGE_EVIDENCE_LIMIT,
    STAGE_QUERY_PREFIX,
    stage_required_kinds,
)
from labrelay.planning.validator import PlanValidationError, validate_plan

logger = logging.getLogger("labrelay.planning.generator")

#: What the handoff read asks for, and how much of it the prompt may carry.
HANDOFF_QUERY = "当前交接目标 已验证状态 下一步 约束"
MAX_HANDOFF_BYTES = 8000

#: The reason a plan stayed a draft, and the reason a model answer could not be read at all.
INSUFFICIENT_CITED_EVIDENCE = "insufficient_cited_evidence"
INVALID_TASK_PAYLOAD = "invalid_task_payload"

NEEDS_MATERIAL_STATUS = "needs_material"
LOCKED_STATUS = "locked"
TODO_STATUS = "todo"

SYSTEM_PROMPT = (
    "生成 LabRelay 学习路径 JSON，顶层字段只有 tasks。"
    "每个任务必须包含 stage、title、objective、prerequisite_task_ids、estimated_minutes、"
    "citations、steps、pitfalls、completion_criteria、evidence_requirements、task_type。"
    "citations 只能复用本次请求提供的 asset_id，不得编造命令、路径、指标或资料；"
    "阶段必须按 0 到 6 排列，实验复现阶段必须同时引用 code、config 和 experiment 三类资料。"
    "某个阶段证据不足时，把该任务的状态设为 needs_material，不要伪造引用。只返回 JSON。"
)


def new_plan_id() -> str:
    """A plan version's own identity, which also namespaces the task ids generated for it."""
    return f"plan-{uuid.uuid4().hex}"


async def generate_learning_plan(
    *,
    repository: Repository,
    engine: SearchEngine,
    context: ContextGateway,
    model: ModelGateway,
    config: PlannerConfig,
    project: Project,
    assessment: Assessment,
) -> LearningPlan:
    """Build, validate and (when it is complete) activate this project's next plan version."""
    _, handoff_context = await context.prepare_context(
        project.project_scope_id, HANDOFF_QUERY, MAX_HANDOFF_BYTES
    )
    stages = await _retrieve_stage_evidence(engine, project)

    plan_id = new_plan_id()
    version = await repository.get_next_plan_version(project.id)
    model_response = await model.generate_json(
        SYSTEM_PROMPT,
        {
            "project": project.model_dump(mode="json"),
            "handoff": handoff_context,
            "assessment": assessment.model_dump(mode="json"),
            "plan_id": plan_id,
            "plan_version": version,
            "stages": stages,
        },
    )

    plan = LearningPlan(
        id=plan_id,
        project_id=project.id,
        version=version,
        status="draft",
        assessment_id=assessment.id,
        tasks=_parse_tasks(model_response, project=project, plan_id=plan_id),
    )
    plan = await validate_plan(repository, config, plan)
    await repository.save_plan(plan)

    gaps = [task for task in plan.tasks if task.status == NEEDS_MATERIAL_STATUS]
    if gaps:
        logger.warning(
            "plan_not_activated plan_id=%s project_id=%s version=%s reason=%s task_ids=%s",
            plan.id,
            project.id,
            plan.version,
            INSUFFICIENT_CITED_EVIDENCE,
            ",".join(task.id for task in gaps),
        )
        return plan
    return await _activate(repository, plan, project)


async def _retrieve_stage_evidence(
    engine: SearchEngine, project: Project
) -> list[dict[str, object]]:
    """One retrieval call per stage, so the model can only cite what was actually found."""
    stages: list[dict[str, object]] = []
    for stage in PLANNING_STAGES:
        hits = await engine.search(
            project.id,
            f"{project.title} {STAGE_QUERY_PREFIX[stage]}",
            stage_required_kinds(stage),
            STAGE_EVIDENCE_LIMIT,
        )
        stages.append(
            {
                "id": stage,
                "required_evidence": stage_required_kinds(stage),
                "evidence": [hit.model_dump(mode="json") for hit in hits],
            }
        )
    return stages


def _parse_tasks(model_response: object, *, project: Project, plan_id: str) -> list[LearningTask]:
    """Read the model's task list into the domain, with this project's identity written last.

    ``project_id`` and ``plan_id`` come from the caller rather than the answer: a plan is stored
    per project, and the task ids it contains were generated for one plan version only.
    """
    raw_tasks = model_response.get("tasks") if isinstance(model_response, dict) else None
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise PlanValidationError(
            "the model answer carries no task list", reason=INVALID_TASK_PAYLOAD
        )
    tasks: list[LearningTask] = []
    for entry in raw_tasks:
        if not isinstance(entry, dict):
            raise PlanValidationError(
                "the model answer carries a task that is not an object",
                reason=INVALID_TASK_PAYLOAD,
            )
        try:
            tasks.append(
                LearningTask.model_validate({**entry, "project_id": project.id, "plan_id": plan_id})
            )
        except ValidationError as exc:
            # Field counts only: the rejected answer is model-authored text and stays out of any
            # response or log line (spec 19.1).
            raise PlanValidationError(
                f"the model answer carries an invalid task ({len(exc.errors())} field errors)",
                reason=INVALID_TASK_PAYLOAD,
            ) from None
    return tasks


async def _activate(repository: Repository, plan: LearningPlan, project: Project) -> LearningPlan:
    """Open the plan and make its roots executable, then point the project at it.

    Only a task with no prerequisites leaves ``locked``; the rest of the chain is unlocked by
    Task 11's progress rules, so a fresh plan never hands a learner stage 4 on day one.
    """
    tasks: list[LearningTask] = []
    for task in plan.tasks:
        if task.status == LOCKED_STATUS and not task.prerequisite_task_ids:
            task = task.model_copy(update={"status": TODO_STATUS})
            await repository.save_task(task)
        tasks.append(task)
    activated = plan.model_copy(update={"status": "active", "tasks": tasks})
    await repository.save_plan(activated)
    await repository.save_project(project.model_copy(update={"active_plan_id": activated.id}))
    return activated
