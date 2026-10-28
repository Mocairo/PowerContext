"""Plan generation: committed Handoff plus retrieved evidence becomes a ``LearningPlan``.

The order below is spec 15.2 and every step earns its place:

1. read the Handoff the leaving researcher confirmed (project scope, not the learner's own notes),
2. ask retrieval for each stage's evidence, so the only ``asset_id`` a model can name is one this
   project actually holds,
3. request strict JSON from the generation gateway,
4. build ``LearningTask`` rows and hand the plan to :mod:`labrelay.planning.validator`,
5. save, and activate **only** when no task still says ``needs_material``.

Two decisions are deliberate. The stored task ids are namespaced server-side from the ``plan_id``
minted before the model call — never from the model's own ids and never from prompt compliance —
because a second plan for the same assessment must not write onto the first plan's primary keys
(the carry-forward from Task 8). And a gap never raises: an under-evidenced baseline is a real,
useful answer, so the plan is kept as a draft, the reason is logged once with a stable code, and
the learner pointer stays where it was.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Sequence

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
from labrelay.planning.validator import MAX_TASK_ID_LENGTH, PlanValidationError, validate_plan

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


#: ``task.id``/``task_events.task_id`` are VARCHAR(64); the namespace tag is a short digest of the
#: plan id so a model-authored suffix still fits inside the column while staying plan-scoped.
TASK_ID_NAMESPACE_CHARS = 12


def new_plan_id() -> str:
    """A plan version's own identity, which also namespaces the task ids generated for it."""
    return f"plan-{uuid.uuid4().hex}"


def _task_id_namespace(plan_id: str) -> str:
    """A stable, plan-scoped tag: every stored task id of this version starts with it.

    It is derived only from ``plan_id`` and never from the model, so two plan versions of one
    project can not collide on a ``learning_task.id`` primary key regardless of what ids the model
    invented (spec 15.2, the Task 8 carry-forward).
    """
    return hashlib.sha256(plan_id.encode("utf-8")).hexdigest()[:TASK_ID_NAMESPACE_CHARS]


def _stored_task_id(namespace: str, raw_key: str) -> str:
    """Fold a model-provided task id into the plan's own id space, capped to the column width.

    Short natural ids (``orientation``, ``paper``) keep their readable suffix behind the namespace
    tag; an id that would overflow VARCHAR(64) is truncated and given a stable digest of the *full*
    raw id, so two different over-long ids never collapse onto the same stored key.
    """
    prefix = f"t{namespace}-"
    candidate = f"{prefix}{raw_key}"
    if len(candidate) <= MAX_TASK_ID_LENGTH:
        return candidate
    digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]
    room = MAX_TASK_ID_LENGTH - len(prefix) - len(digest) - 1
    return f"{prefix}{raw_key[:room]}-{digest}"


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


def _parse_tasks(
    model_response: object,
    *,
    project: Project,
    plan_id: str,
    carried: dict[str, str] | None = None,
) -> list[LearningTask]:
    """Read the model's task list into the domain, with this project's identity written last.

    ``project_id`` and ``plan_id`` come from the caller rather than the answer. Crucially, the
    *stored* task id is derived server-side from ``plan_id`` too, and every prerequisite reference
    is rewritten through the same mapping: a model that names its tasks ``orientation``, ``paper``
    cannot make two plan versions collide on the primary key, and the id it "used" is only ever an
    input to the mapping, never what gets persisted.

    ``carried`` (Task 11's replanning) pre-seeds that mapping with ``source-plan id -> new id`` for
    the tasks already finished in the plan being replaced. A model task naming one of them is the
    same work, already done, so it is dropped rather than duplicated; a prerequisite naming one
    resolves to the carried row instead of to a task that does not exist in this version.
    """
    raw_tasks = model_response.get("tasks") if isinstance(model_response, dict) else None
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise PlanValidationError(
            "the model answer carries no task list", reason=INVALID_TASK_PAYLOAD
        )

    namespace = _task_id_namespace(plan_id)
    already_carried = dict(carried or {})
    entries: list[tuple[dict[str, object], list[object]]] = []
    id_map: dict[str, str] = dict(already_carried)
    for index, entry in enumerate(raw_tasks):
        if not isinstance(entry, dict):
            raise PlanValidationError(
                "the model answer carries a task that is not an object",
                reason=INVALID_TASK_PAYLOAD,
            )
        payload = dict(entry)
        raw_key = _raw_task_key(payload.get("id"), index)
        if raw_key in already_carried:
            continue
        # A fresh task's status is not the model's to declare: ``done``/``skipped`` would be history
        # with no evidence event behind it, and ``source_task_id`` is reserved for carried replans.
        payload.pop("status", None)
        payload.pop("source_task_id", None)
        # First-seen wins: two model tasks that share a raw id keep the same stored id so the
        # duplicate is reported by the validator, not silently merged apart here.
        stored_id = id_map.setdefault(raw_key, _stored_task_id(namespace, raw_key))
        payload["id"] = stored_id
        entries.append((payload, _raw_prerequisites(payload)))

    tasks: list[LearningTask] = []
    for payload, raw_prerequisites in entries:
        payload["prerequisite_task_ids"] = [
            _rewrite_prerequisite(namespace, raw_key, id_map) for raw_key in raw_prerequisites
        ]
        try:
            tasks.append(
                LearningTask.model_validate(
                    {**payload, "project_id": project.id, "plan_id": plan_id}
                )
            )
        except ValidationError as exc:
            # Field counts only: the rejected answer is model-authored text and stays out of any
            # response or log line (spec 19.1).
            raise PlanValidationError(
                f"the model answer carries an invalid task ({len(exc.errors())} field errors)",
                reason=INVALID_TASK_PAYLOAD,
            ) from None
    return tasks


def _raw_prerequisites(payload: dict[str, object]) -> list[object]:
    """Read ``prerequisite_task_ids`` as a sequence or refuse the payload; never iterate a scalar.

    The conversion this guards used to be ``list(payload.get(...) or [])``: a number or boolean
    reached ``list()`` and raised ``TypeError`` (answered as a redacted 500), a string would have
    silently split into per-character prerequisites, and an explicit ``null`` was folded into "no
    prerequisites" — a different plan than the model described. A JSON list/tuple is the only
    shape that maps; an absent key keeps the domain default. Everything else is a task payload
    this service cannot read, so it leaves as ``invalid_task_payload`` (422) like the rest.
    """
    if "prerequisite_task_ids" not in payload:
        return []
    value = payload["prerequisite_task_ids"]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    raise PlanValidationError(
        "the model answer carries a task whose prerequisite_task_ids is not a list",
        reason=INVALID_TASK_PAYLOAD,
    )


def _raw_task_key(raw_id: object, index: int) -> str:
    """The model's own id used only as a mapping key; a missing one falls back to its position."""
    text = raw_id.strip() if isinstance(raw_id, str) else (str(raw_id).strip() if raw_id else "")
    return text or f"task-{index}"


def _rewrite_prerequisite(namespace: str, raw_key: object, id_map: dict[str, str]) -> str:
    """Map one model prerequisite reference into the plan's id space.

    A reference to a task the model actually named resolves to that task's stored id; a stray one
    is namespaced the same way so it stays visibly foreign and the validator still reports it as an
    unknown prerequisite rather than accidentally matching a real row.
    """
    key = raw_key.strip() if isinstance(raw_key, str) else (str(raw_key).strip() if raw_key else "")
    if key in id_map:
        return id_map[key]
    return _stored_task_id(namespace, key or "unknown")


async def _activate(repository: Repository, plan: LearningPlan, project: Project) -> LearningPlan:
    """Open the plan, make its roots executable, supersede the previous active plan, repoint.

    The validator guarantees a prerequisite-free task is a stage 0 task, so only entry-point
    stages ever leave ``locked`` here; the rest of the chain is unlocked by Task 11's progress
    rules, so a fresh plan never hands a learner stage 4 on day one.

    Superseding happens before the project pointer moves: a project must never hold two plans
    with ``status == "active"`` at once. A prior draft/superseded plan is left untouched; only a
    previous *active* plan is demoted, which keeps re-generation from corrupting active-plan state
    even though Task 11 owns the richer replanning rules.
    """
    if project.active_plan_id and project.active_plan_id != plan.id:
        previous = await repository.get_plan(project.active_plan_id)
        if previous is not None and previous.status == "active":
            await repository.save_plan(previous.model_copy(update={"status": "superseded"}))

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
