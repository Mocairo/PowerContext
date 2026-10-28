"""Replanning: a plan with finished work in it becomes the source of a new immutable version.

Spec 16.3 and FR-PRG-006 decide the shape of this module. The previous version, its task rows and
every TaskEvent it holds are read-only — a replan mints ``version + 1`` under a fresh plan id and
copies the finished work into *that* id space, because writing the copy back onto the source row is
exactly the "overwrite the old plan's primary key" bug Task 10's namespacing was added to prevent.

The order is what makes a failed replan harmless: the retrieval call, the model call, the copy and
``validate_plan`` all run before the first write, so a rejected answer leaves the active plan and
the whole event trail untouched. Activation then reuses
:func:`labrelay.planning.generator._activate` — "supersede the plan the project points at, open
this version's roots, move the pointer" is one rule with one owner, not two — and the learner-memory
note goes last, since a memory outage must never turn into a half-applied replan (spec 20).
"""

from __future__ import annotations

import logging
from typing import Any

from labrelay.config import PlannerConfig
from labrelay.domain.models import LearningPlan, LearningTask, Project, TaskEvent
from labrelay.domain.ports import ModelGateway, Repository, SearchEngine
from labrelay.learning.progress import (
    TERMINAL_STATUSES,
    apply_unlocks,
    next_actionable,
)
from labrelay.planning.generator import (
    _activate,
    _parse_tasks,
    _retrieve_stage_evidence,
    _stored_task_id,
    _task_id_namespace,
    new_plan_id,
)
from labrelay.planning.validator import validate_plan

logger = logging.getLogger("labrelay.learning.replan")

#: What a replan asks the model for: the remainder only. Finished work is history, not a proposal.
REPLAN_SYSTEM_PROMPT = (
    "在现有学习路径基础上重新规划，只输出尚未完成的任务，顶层字段只有 tasks。"
    "已完成或已跳过的任务不得重复提出，也不要改写它们的目标或引用。"
    "每个任务必须包含 stage、title、objective、prerequisite_task_ids、estimated_minutes、"
    "citations、steps、pitfalls、completion_criteria、evidence_requirements、task_type；"
    "citations 只能复用本次请求 stages 里给出的 asset_id，不得编造命令、路径、指标或资料；"
    "阶段必须按 0 到 6 非递减排列，且不得低于已完成任务的阶段；"
    "实验复现阶段必须同时引用 code、config 和 experiment 三类资料。"
    "证据不足的任务把状态设为 needs_material，不要伪造引用。只返回 JSON。"
)

#: How much of the past one replan request may carry. Events travel as a digest, not a transcript:
#: an unbounded trail would make the prompt grow with the project, and a payload the model can read
#: is not where learner prose belongs (spec 19.1).
MAX_REPLAN_EVENTS = 40


def completed_view(plan: LearningPlan, evidence: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The finished tasks, with the evidence that closed each one out."""
    return [
        {
            "id": task.id,
            "stage": task.stage,
            "title": task.title,
            "objective": task.objective,
            "status": task.status,
            "completion_criteria": task.completion_criteria,
            "evidence": evidence.get(task.id, {}),
        }
        for task in plan.tasks
        if task.status in TERMINAL_STATUSES
    ]


def remaining_view(plan: LearningPlan) -> list[dict[str, Any]]:
    """What the new version is allowed to replace, named by the ids the learner can see."""
    return [
        {
            "id": task.id,
            "stage": task.stage,
            "title": task.title,
            "status": task.status,
            "prerequisite_task_ids": list(task.prerequisite_task_ids),
        }
        for task in plan.tasks
        if task.status not in TERMINAL_STATUSES
    ]


def event_digest(events: list[TaskEvent]) -> list[dict[str, Any]]:
    """The last few things that happened, as identifiers and status names only."""
    return [
        {
            "task_id": event.task_id,
            "plan_id": event.plan_id,
            "event_type": event.event_type,
            "to": event.payload.get("to"),
        }
        for event in events[-MAX_REPLAN_EVENTS:]
    ]


def carry_forward(
    tasks: list[LearningTask], *, project: Project, plan_id: str
) -> tuple[list[LearningTask], dict[str, str]]:
    """Copy the finished tasks into the new version's own id space.

    Returns the copies plus the ``source id -> new id`` map the model answer is rewritten through,
    so a remainder that depends on work completed two versions ago still points at a task this
    version actually holds. Everything that makes the record meaningful travels untouched —
    objective, stage, citations, evidence requirements, the status itself — and only the identity
    changes, with ``source_task_id`` keeping the original id reachable (spec 16.3).

    A prerequisite a finished task still names is copied along with it even when that prerequisite
    is itself unfinished, which can happen when a blocked task was skipped around. Without it the
    new version would reference a task it does not hold, and the validator would refuse the whole
    replan for one stale edge the learner never asked about.
    """
    namespace = _task_id_namespace(plan_id)
    by_id = {task.id: task for task in tasks}
    order = {task.id: index for index, task in enumerate(tasks)}

    needed: dict[str, LearningTask] = {}
    stack = [task for task in tasks if task.status in TERMINAL_STATUSES]
    while stack:
        task = stack.pop()
        if task.id in needed:
            continue
        needed[task.id] = task
        for prerequisite in task.prerequisite_task_ids:
            referenced = by_id.get(prerequisite)
            if referenced is not None and referenced.id not in needed:
                stack.append(referenced)

    id_map = {task_id: _stored_task_id(namespace, task_id) for task_id in needed}
    carried: list[LearningTask] = []
    for task_id in sorted(needed, key=lambda key: (needed[key].stage, order[key])):
        task = needed[task_id]
        carried.append(
            task.model_copy(
                update={
                    "id": id_map[task_id],
                    "project_id": project.id,
                    "plan_id": plan_id,
                    "source_task_id": task.id,
                    "prerequisite_task_ids": [
                        id_map[prerequisite]
                        for prerequisite in task.prerequisite_task_ids
                        if prerequisite in id_map
                    ],
                },
                deep=True,
            )
        )
    return carried, id_map


async def build_replanned_plan(
    *,
    repository: Repository,
    engine: SearchEngine,
    model: ModelGateway,
    config: PlannerConfig,
    project: Project,
    source: LearningPlan,
    reason: str,
    evidence: dict[str, dict[str, Any]],
    events: list[TaskEvent],
) -> LearningPlan:
    """Ask the model for the remainder and assemble the next version, writing nothing.

    Every refusal — the model call, the payload parse, the citation, dependency and stage checks —
    happens here, which is what leaves the active plan intact when the answer is not a plan.
    """
    plan_id = new_plan_id()
    version = await repository.get_next_plan_version(project.id)
    stages = await _retrieve_stage_evidence(engine, project)
    model_response = await model.generate_json(
        REPLAN_SYSTEM_PROMPT,
        {
            "project": project.model_dump(mode="json"),
            "reason": reason,
            "source_plan_id": source.id,
            "source_plan_version": source.version,
            "plan_id": plan_id,
            "plan_version": version,
            "assessment_id": source.assessment_id,
            "completed_tasks": completed_view(source, evidence),
            "remaining_tasks": remaining_view(source),
            "recent_events": event_digest(events),
            "stages": stages,
        },
    )

    carried, id_map = carry_forward(source.tasks, project=project, plan_id=plan_id)
    # The answer is namespaced the same way the first generation's was: an id the model invents is
    # only ever a mapping key, and a prerequisite naming a finished task resolves to the copy this
    # version carries rather than to a row that no longer belongs to the active plan.
    remainder = _parse_tasks(model_response, project=project, plan_id=plan_id, carried=id_map)
    draft = LearningPlan(
        id=plan_id,
        project_id=project.id,
        version=version,
        status="draft",
        assessment_id=source.assessment_id,
        # Finished work first: the stage order the validator checks is the order the learner reads.
        tasks=[*carried, *remainder],
    )
    return await validate_plan(repository, config, draft)


async def activate_replan(
    repository: Repository, plan: LearningPlan, project: Project
) -> LearningPlan:
    """Make the new version active, then reopen the chain the carried history already earned.

    A stage-1 task whose stage-0 prerequisite was completed in the *previous* version must not stay
    locked just because that completion belongs to a plan that is now superseded. The pass runs
    without an ``unlocked_by`` reason, so it changes no history: reopening a resumed chain is not
    an event that happened to the learner (FR-PRG-006).
    """
    activated = await _activate(repository, plan, project)
    if not any(task.status in TERMINAL_STATUSES for task in activated.tasks):
        return activated
    await apply_unlocks(repository, activated)
    return await repository.get_plan(activated.id) or activated


def replan_summary(plan: LearningPlan) -> str:
    """The one note a new session needs after a replan: how far it got and what comes next."""
    done = [task for task in plan.tasks if task.status in TERMINAL_STATUSES]
    blocked = [task.title for task in plan.tasks if task.status == "blocked"]
    needs_material = [task.title for task in plan.tasks if task.status == "needs_material"]
    next_task = next_actionable(plan)
    parts = [
        f"学习路径已重规划为版本 {plan.version}：已完成 {len(done)} 个任务，"
        f"当前共 {len(plan.tasks)} 个任务。"
    ]
    if next_task is not None:
        parts.append(f"下一任务「{next_task.title}」（阶段 {next_task.stage}）。")
    if blocked:
        parts.append("仍在阻塞：" + "、".join(blocked) + "。")
    if needs_material:
        parts.append("资料不足：" + "、".join(needs_material) + "。")
    return "".join(parts)


__all__ = [
    "MAX_REPLAN_EVENTS",
    "REPLAN_SYSTEM_PROMPT",
    "activate_replan",
    "build_replanned_plan",
    "carry_forward",
    "completed_view",
    "event_digest",
    "remaining_view",
    "replan_summary",
]
