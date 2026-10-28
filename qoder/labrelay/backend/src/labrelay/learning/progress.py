"""Learning layer: progress rules, the recovered position and the reverse Handoff draft.

Task 5 owns the raw transition (:meth:`LabRelayService.update_task_status`: the domain table, one
append-only ``status_changed`` event, the learner-memory sync and its degradation record). This
module owns the four things that layer sits on top of, and that only Task 11's API touches:

* 「done」 carries the evidence the task declared, 「skipped」 carries a reason *and* the
  assessment or user confirmation behind it, and 「blocked」 carries its reason — spec 16.1 and
  FR-PRG-008 make all three machine-readable rather than a checkbox, because a skip that leaves no
  trace is a hole in the path the next session has to walk through,
* a learner may open a 「locked」 task only when every prerequisite is already done or skipped; the
  chain itself opens through :func:`apply_unlocks`, one completion at a time, so a stage-4 task is
  never reachable on day one (spec 16.1, 15.2),
* :func:`build_learning_context` answers 「where was I?」 for a brand new session: the Learner Scope
  text when PowerContext has it, and an honest local summary built from the repository when it does
  not — an outage may cost the prose but never the facts (spec 20),
* :func:`build_reverse_handoff_draft` assembles Task 9's four-field draft out of the durable event
  trail, so the export is a record of what happened rather than a re-telling.

Nothing here writes a TaskEvent other than the evidence and unlock records; the status history
stays exactly as Task 5 appended it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from labrelay.domain.models import LearningPlan, LearningTask, Project, TaskEvent
from labrelay.domain.ports import Repository

logger = logging.getLogger("labrelay.learning.progress")

#: The stable ``reason`` codes the API returns when a progress write is refused (spec 17.2, 17.3).
#: A client branches on these, never on the message text.
MISSING_EVIDENCE = "missing_evidence"
SKIP_REQUIRES_CONFIRMATION = "skip_requires_confirmation"
PREREQUISITES_INCOMPLETE = "prerequisites_incomplete"
STALE_PLAN = "stale_plan"
#: The REST layer's answer for a pair of statuses the domain table itself refuses; the three above
#: are the writes this module refuses on top of a table-legal transition.
ILLEGAL_TRANSITION = "illegal_transition"

#: The two event types this layer appends. ``status_changed`` and ``memory_sync_failed`` stay
#: Task 5's, and a replan appends none of its own: the history is read, never rewritten.
EVIDENCE_SUBMITTED_EVENT = "evidence_submitted"
TASK_UNLOCKED_EVENT = "task_unlocked"

#: A task whose own work is over. Both open the successors; neither is ever rewritten.
TERMINAL_STATUSES = frozenset({"done", "skipped"})
#: What a learner may still act on, in the order a recovered session should offer it.
OPEN_STATUSES = ("in_progress", "todo", "blocked", "needs_material")

LOCKED_STATUS = "locked"
TODO_STATUS = "todo"

#: The two keys that make a skip auditable (FR-PRG-008): at least one must be present.
SKIP_CONFIRMATION_KEYS = ("skip_confirmed_by", "assessment_id")

#: Task 9's ``HandoffDraft`` caps, kept here so a long project cannot outgrow the wire format.
MAX_HANDOFF_ITEMS = 64
MAX_HANDOFF_CITATIONS = 64

#: What a progress read asks the Learner Scope for, and how much of it may carry.
LEARNER_CONTEXT_QUERY = "当前阶段 已完成任务 阻塞原因 下一任务"
MAX_LEARNER_CONTEXT_BYTES = 8000


class TaskTransitionRejected(ValueError):
    """A progress write the learning rules refuse, before a single row is touched.

    ``reason`` is the machine-readable half the REST layer returns; the message may name an
    identifier or a missing evidence key because the learner has to be told what to fill in. Only
    request-body text reaches it — never model output — so it is safe to log and to quote back.
    """

    def __init__(self, message: str, *, reason: str, missing_count: int = 0) -> None:
        super().__init__(message)
        self.reason = reason
        self.missing_count = missing_count


class NoActivePlanError(ValueError):
    """The project has no plan to progress through, so there is nothing to report or replan."""

    def __init__(self, project_id: str) -> None:
        super().__init__(f"project {project_id} has no active plan")
        self.project_id = project_id


# --- progress rules ------------------------------------------------------------


def check_progress_rule(
    task: LearningTask,
    status: str,
    evidence: Mapping[str, Any] | None,
    plan: LearningPlan | None,
) -> None:
    """Refuse a progress write the domain transition table alone would have allowed.

    Raised before anything is stored, so a refusal leaves the task, the event trail and the memory
    service exactly as they were. The legality of the transition itself stays where Task 5 put it.
    """
    payload = dict(evidence or {})

    if task.status == LOCKED_STATUS and status == TODO_STATUS:
        unfinished = _unfinished_prerequisites(task, plan)
        if unfinished:
            raise TaskTransitionRejected(
                f"task {task.id} is still locked; unfinished prerequisites: "
                f"{', '.join(unfinished)}",
                reason=PREREQUISITES_INCOMPLETE,
            )

    if status == "done":
        missing = [
            requirement
            for requirement in task.evidence_requirements
            if not _has_value(payload, requirement)
        ]
        has_any = any(_has_value(payload, key) for key in payload)
        if missing or not has_any:
            raise TaskTransitionRejected(
                f"completing task {task.id} needs evidence: {', '.join(missing)}",
                reason=MISSING_EVIDENCE,
                missing_count=len(missing) or 1,
            )

    if status == "skipped":
        confirmed = [key for key in SKIP_CONFIRMATION_KEYS if _has_value(payload, key)]
        if not _has_value(payload, "reason") or not confirmed:
            raise TaskTransitionRejected(
                f"skipping task {task.id} needs a reason and one of "
                f"{', '.join(SKIP_CONFIRMATION_KEYS)}",
                reason=SKIP_REQUIRES_CONFIRMATION,
            )


def _has_value(payload: Mapping[str, Any], key: str) -> bool:
    """A declared-but-empty field is not evidence; ``None``/``""``/``[]`` all read as missing."""
    value = payload.get(key)
    if value is None:
        return False
    if isinstance(value, str | list | dict | tuple):
        return bool(value.strip()) if isinstance(value, str) else bool(value)
    if isinstance(value, bool):
        return value
    return True


def _prerequisites(task: LearningTask, plan: LearningPlan | None) -> dict[str, LearningTask]:
    by_id = {item.id: item for item in (plan.tasks if plan is not None else [])}
    return {pid: by_id[pid] for pid in task.prerequisite_task_ids if pid in by_id}


def _unfinished_prerequisites(task: LearningTask, plan: LearningPlan | None) -> list[str]:
    """What still stands between a locked task and the learner, including references it cannot see.

    A prerequisite this plan does not hold is unfinished by definition: refusing the write is the
    safe answer, because opening a task the validator could not have reached is exactly the chain
    break Task 10's activation invariant was added to prevent.
    """
    by_id = _prerequisites(task, plan)
    return [
        pid
        for pid in task.prerequisite_task_ids
        if by_id.get(pid) is None or by_id[pid].status not in TERMINAL_STATUSES
    ]


def unlocks_for(plan: LearningPlan) -> list[LearningTask]:
    """The locked tasks whose prerequisites are all finished, in plan order.

    One pass is enough by construction: a task only reaches ``todo`` here, and ``todo`` is not a
    finished prerequisite, so the chain never cascades past its next link.
    """
    by_id = {task.id: task for task in plan.tasks}
    ready: list[LearningTask] = []
    for task in plan.tasks:
        if task.status != LOCKED_STATUS or not task.prerequisite_task_ids:
            continue
        if all(
            by_id.get(pid) is not None and by_id[pid].status in TERMINAL_STATUSES
            for pid in task.prerequisite_task_ids
        ):
            ready.append(task)
    return ready


async def apply_unlocks(
    repository: Repository, plan: LearningPlan, *, unlocked_by: str | None = None
) -> list[str]:
    """Open every ready task, keep the stored plan in step, and report what opened.

    ``unlocked_by`` is the completion the record credits, and it is also the switch: with a
    progress write the opening is learner-visible history, so each task gets its own append-only
    event. Replanning passes ``None`` because opening a resumed chain is not learner activity — an
    event for it would let a plan version claim progress that no session ever made (FR-PRG-006).
    """
    ready = unlocks_for(plan)
    if not ready:
        return []
    tasks = {task.id: task for task in plan.tasks}
    opened: list[str] = []
    for task in ready:
        updated = task.model_copy(update={"status": TODO_STATUS})
        tasks[task.id] = updated
        await repository.save_task(updated)
        if unlocked_by is not None:
            await repository.append_event(
                TaskEvent(
                    id=f"event-{uuid4().hex}",
                    project_id=task.project_id,
                    plan_id=task.plan_id,
                    task_id=task.id,
                    event_type=TASK_UNLOCKED_EVENT,
                    payload={"task_id": task.id, "unlocked_by": unlocked_by},
                    occurred_at=datetime.now(UTC).isoformat(),
                )
            )
        opened.append(task.id)
    await repository.save_plan(plan.model_copy(update={"tasks": list(tasks.values())}))
    return opened


async def record_evidence(repository: Repository, task: LearningTask, evidence: Mapping[str, Any]):
    """Append the submitted evidence as its own record, and hand back the event.

    Separate from the ``status_changed`` event because the two outlive each other: a replan keeps
    the evidence while the transition belongs to a plan version that is no longer active, and
    spec 11.1 says neither may be edited in place.
    """
    event = TaskEvent(
        id=f"event-{uuid4().hex}",
        project_id=task.project_id,
        plan_id=task.plan_id,
        task_id=task.id,
        event_type=EVIDENCE_SUBMITTED_EVENT,
        payload={"task_id": task.id, "evidence": dict(evidence)},
        occurred_at=datetime.now(UTC).isoformat(),
    )
    await repository.append_event(event)
    return event


# --- reading the trail back -----------------------------------------------------


def evidence_by_task(events: list[TaskEvent]) -> dict[str, dict[str, Any]]:
    """The newest submitted evidence per task; a later submission replaces the earlier one."""
    collected: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.event_type != EVIDENCE_SUBMITTED_EVENT:
            continue
        payload = event.payload.get("evidence")
        if isinstance(payload, Mapping):
            collected[event.task_id] = dict(payload)
    return collected


def reasons_by_task(events: list[TaskEvent], status: str) -> dict[str, str]:
    """Why each task last reached ``status``, read out of its own transition event."""
    reasons: dict[str, str] = {}
    for event in events:
        if event.event_type != "status_changed" or event.payload.get("to") != status:
            continue
        reason = event.payload.get("reason")
        if reason:
            reasons[event.task_id] = str(reason)
    return reasons


def next_actionable(plan: LearningPlan) -> LearningTask | None:
    """The task a recovered learner is offered first: the resumed one, else the next to start."""
    for status in OPEN_STATUSES:
        for task in plan.tasks:
            if task.status == status:
                return task
    return None


def _action_text(task: LearningTask) -> str:
    return f"{task.title}：{task.objective}"


def _event_key(task: LearningTask) -> str:
    """The id this task's history was recorded under.

    A carried replan task is a copy with a new id; its evidence and transition events still name
    the source task. Reading only the copy's id would make finished work look unevidenced.
    """
    return task.source_task_id or task.id


def _task_brief(task: LearningTask | None) -> dict[str, Any] | None:
    if task is None:
        return None
    return {
        "id": task.id,
        "title": task.title,
        "objective": task.objective,
        "stage": task.stage,
        "status": task.status,
        "estimated_minutes": task.estimated_minutes,
        "citations": [citation.model_dump(mode="json") for citation in task.citations],
    }


def local_progress_summary(project: Project, plan: LearningPlan, events: list[TaskEvent]) -> str:
    """The note a session reads when the Learner Scope has nothing (or answered at all).

    Built from the same rows the dashboard renders, so 「无历史」 and 「进度丢失」 stay
    distinguishable: this text always describes real stored state and never invents any (spec 20).
    """
    evidence = evidence_by_task(events)
    blocked_reasons = reasons_by_task(events, "blocked")
    skipped_reasons = reasons_by_task(events, "skipped")
    finished = [task for task in plan.tasks if task.status in TERMINAL_STATUSES]
    lines = [
        f"项目「{project.title}」本地进度：Plan 版本 {plan.version}，共 {len(plan.tasks)} 个任务。"
    ]
    for task in finished:
        key = _event_key(task)
        evidence_summary = evidence.get(key, {}).get("summary")
        skip_reason = skipped_reasons.get(key)
        lines.append(
            f"「{task.title}」"
            + (
                f"（证据：{evidence_summary}）"
                if evidence_summary
                else (f"（跳过依据：{skip_reason}）" if skip_reason else "")
            )
        )
    next_task = next_actionable(plan)
    if next_task is not None:
        lines.append(
            f"下一任务：「{next_task.title}」（阶段 {next_task.stage}，{next_task.status}）。"
        )
    blocked = [task for task in plan.tasks if task.status == "blocked"]
    if blocked:
        lines.append(
            "阻塞："
            + "、".join(
                f"「{task.title}」原因为 {blocked_reasons.get(task.id, '未说明')}"
                for task in blocked
            )
        )
    return "\n".join(lines)


def build_learning_context(
    project: Project,
    plan: LearningPlan,
    events: list[TaskEvent],
    memory: tuple[str | None, str, bool],
) -> dict[str, Any]:
    """Where this learner stands, assembled from the plan plus the durable event trail.

    ``memory`` is the ``(text, source, available)`` triple the caller resolved from the Learner
    Scope: PowerContext's own words when it has any, the local summary when it has none or is down.
    The stage, the next task, what is finished and what is blocked always come from the repository,
    which is what makes FR-PRG-005 hold during an outage.
    """
    text, source, available = memory
    evidence = evidence_by_task(events)
    blocked_reasons = reasons_by_task(events, "blocked")
    skipped_reasons = reasons_by_task(events, "skipped")
    next_task = next_actionable(plan)
    finished = [task for task in plan.tasks if task.status in TERMINAL_STATUSES]
    return {
        "project_id": project.id,
        "plan_id": plan.id,
        "plan_version": plan.version,
        "current_stage": next_task.stage
        if next_task is not None
        else (finished[-1].stage if finished else plan.tasks[-1].stage if plan.tasks else 0),
        "next_task": _task_brief(next_task),
        "completed": [
            {
                "task_id": task.id,
                "title": task.title,
                "stage": task.stage,
                "status": task.status,
                "evidence": evidence.get(_event_key(task), {}),
                "reason": skipped_reasons.get(_event_key(task), "")
                if task.status == "skipped"
                else "",
            }
            for task in finished
        ],
        "blockers": [
            {
                "task_id": task.id,
                "title": task.title,
                "stage": task.stage,
                "reason": blocked_reasons.get(task.id, ""),
            }
            for task in plan.tasks
            if task.status == "blocked"
        ],
        "progress": {"done": len(finished), "total": len(plan.tasks)},
        "memory_summary": text or local_progress_summary(project, plan, events),
        "memory_source": source,
        "memory_available": available,
    }


async def read_learner_memory(context: Any, project: Project) -> tuple[str | None, str, bool]:
    """Ask the Learner Scope what it remembers, and say so honestly when it cannot.

    Three answers, not two: text plus ``powercontext`` when it has history, nothing plus
    ``repository`` when the scope is genuinely empty (spec 20: 「明确显示无历史，不伪造进度」), and
    ``repository`` with ``available=False`` when the service is unreachable — the flag is what lets
    the UI say 「跨会话记忆未同步」 instead of implying the learner never got that far.
    """
    try:
        _, text = await context.prepare_context(
            project.learner_scope_id, LEARNER_CONTEXT_QUERY, MAX_LEARNER_CONTEXT_BYTES
        )
    except Exception as exc:
        # Broad on purpose, and the same rule Task 5 set for a memory *write*: this read's facts all
        # come from the repository, so no gateway answer -- transport failure, a refused scope, an
        # HTTP 4xx nobody mapped -- is allowed to turn 「where was I?」 into a 500. Only the
        # exception class is logged; a gateway message can echo a URL or a token (spec 19.1).
        logger.warning(
            "memory read unavailable operation=learning_context project_id=%s scope_id=%s "
            "exception_class=%s",
            project.id,
            project.learner_scope_id,
            type(exc).__name__,
        )
        return (None, "repository", False)
    return (text or None, "powercontext" if text else "repository", True)


def build_reverse_handoff_draft(
    project: Project, plan: LearningPlan, events: list[TaskEvent]
) -> dict[str, Any]:
    """The four-field reverse Handoff, read entirely out of the stored event trail.

    The shape is exactly Task 9's :class:`~labrelay.application.handoff.HandoffDraft`, so confirming
    it goes through the Handoff service that already exists: this function proposes, it never
    commits, and the learner still has to confirm before a PowerContext revision is made.
    """
    evidence = evidence_by_task(events)
    blocked_reasons = reasons_by_task(events, "blocked")
    skipped_reasons = reasons_by_task(events, "skipped")
    finished = [task for task in plan.tasks if task.status in TERMINAL_STATUSES]
    blockers = [task for task in plan.tasks if task.status == "blocked"]
    next_task = next_actionable(plan)

    state: list[dict[str, Any]] = []
    for task in finished:
        key = _event_key(task)
        citations = task.citations
        if len(citations) > MAX_HANDOFF_CITATIONS:
            dropped = len(citations) - MAX_HANDOFF_CITATIONS
            logger.warning(
                "citation overflow operation=reverse_handoff_draft project_id=%s task_id=%s "
                "dropped=%s",
                project.id,
                task.id,
                dropped,
            )
            citations = citations[:MAX_HANDOFF_CITATIONS]
        if task.status == "skipped":
            text = f"已跳过任务「{task.title}」：{skipped_reasons.get(key, '经确认跳过')}"
        else:
            summary = evidence.get(key, {}).get("summary") or "已提交完成证据"
            text = f"已完成任务「{task.title}」，证据：{summary}"
        state.append(
            {
                "text": text,
                "citations": [citation.model_dump(mode="json") for citation in citations],
            }
        )
    if not state:
        stage = plan.tasks[0].stage if plan.tasks else 0
        state.append(
            {"text": f"「{project.title}」尚无已完成任务，当前处于阶段 {stage}。", "citations": []}
        )

    omissions = [
        f"任务「{task.title}」受阻：{blocked_reasons.get(task.id, '原因未说明')}"
        for task in blockers
    ]
    omissions += [
        f"任务「{task.title}」资料不足，补齐前不可开始"
        for task in plan.tasks
        if task.status == "needs_material"
    ]

    all_finished = all(task.status in TERMINAL_STATUSES for task in plan.tasks)
    return {
        "objective": f"{project.title} 项目学习接手",
        "state": state[:MAX_HANDOFF_ITEMS],
        "next_action": _action_text(next_task) if next_task is not None else "等待交接人补充资料",
        "omissions": omissions[:MAX_HANDOFF_ITEMS],
        "disposition": (
            "blocked"
            if blockers or (next_task is None and not all_finished)
            else "complete"
            if all_finished
            else "continuable"
        ),
        "provenance": {
            "plan_id": plan.id,
            "plan_version": plan.version,
            "events": len(events),
            "completed": len(finished),
        },
    }


async def require_active_plan(repository: Repository, project_id: str) -> LearningPlan:
    """The project's active plan, or the one refusal every Task 11 read and replan shares."""
    plan = await repository.get_active_plan(project_id)
    if plan is None:
        raise NoActivePlanError(project_id)
    return plan


__all__ = [
    "EVIDENCE_SUBMITTED_EVENT",
    "LEARNER_CONTEXT_QUERY",
    "MISSING_EVIDENCE",
    "NoActivePlanError",
    "PREREQUISITES_INCOMPLETE",
    "SKIP_REQUIRES_CONFIRMATION",
    "TASK_UNLOCKED_EVENT",
    "TERMINAL_STATUSES",
    "TaskTransitionRejected",
    "apply_unlocks",
    "build_learning_context",
    "build_reverse_handoff_draft",
    "check_progress_rule",
    "evidence_by_task",
    "local_progress_summary",
    "next_actionable",
    "read_learner_memory",
    "reasons_by_task",
    "record_evidence",
    "require_active_plan",
    "unlocks_for",
]
