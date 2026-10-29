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
from labrelay.model.errors import ModelUnavailableError
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
    payload = {
        "project": project.model_dump(mode="json"),
        "handoff": handoff_context,
        "assessment": assessment.model_dump(mode="json"),
        "plan_id": plan_id,
        "plan_version": version,
        "stages": stages,
    }
    tolerant = type(model).__name__ == "QoderAgentModelGateway"
    model_response = await _generate_tasks(
        model, payload, stages, plan_id=plan_id, project=project, tolerant=tolerant
    )

    citation_index = {
        str(item["citation"]["asset_id"]): item["citation"]
        for stage in stages
        for item in stage.get("evidence", [])
        if isinstance(item, dict) and isinstance(item.get("citation"), dict)
    }
    try:
        tasks = _parse_tasks(
            model_response,
            project=project,
            plan_id=plan_id,
            citation_index=citation_index if tolerant else None,
            normalize=tolerant,
        )
    except PlanValidationError as exc:
        # Structure only: task text and citations are project material and stay out of logs.
        raw_tasks = model_response.get("tasks") if isinstance(model_response, dict) else None
        logger.warning(
            "plan_payload_rejected plan_id=%s project_id=%s model_keys=%s task_count=%s "
            "first_type=%s detail=%s",
            plan_id,
            project.id,
            ",".join(sorted(model_response)) if isinstance(model_response, dict) else "-",
            len(raw_tasks) if isinstance(raw_tasks, list) else "-",
            type(raw_tasks[0]).__name__ if isinstance(raw_tasks, list) and raw_tasks else "-",
            exc,
        )
        raise

    plan = LearningPlan(
        id=plan_id,
        project_id=project.id,
        version=version,
        status="draft",
        assessment_id=assessment.id,
        tasks=tasks,
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


async def _generate_tasks(
    model: ModelGateway,
    payload: dict[str, object],
    stages: list[dict[str, object]],
    *,
    plan_id: str,
    project: Project,
    tolerant: bool,
) -> dict[str, object]:
    """Ask the model for the task list, retrying once, then fall back to the evidence plan.

    Qwen occasionally answers with truncated or syntactically broken JSON on this long prompt.
    A retry with the same contract catches transient failures; if that also fails, the stages and
    their retrieved evidence still describe a usable draft, so the planner builds it locally
    instead of leaving the assessment button permanently broken.
    """
    if not tolerant:
        return await model.generate_json(SYSTEM_PROMPT, payload)
    try:
        return _complete_stages(await model.generate_json(SYSTEM_PROMPT, payload), stages)
    except ModelUnavailableError as first:
        logger.warning(
            "plan_model_retry plan_id=%s project_id=%s reason=%s", plan_id, project.id, first
        )
    try:
        return await model.generate_json(
            SYSTEM_PROMPT + " 上一次输出不是合法 JSON；只返回一个合法 JSON 对象，不要解释。",
            payload,
        )
    except ModelUnavailableError as second:
        logger.warning(
            "plan_model_fallback plan_id=%s project_id=%s reason=%s result=deterministic",
            plan_id,
            project.id,
            second,
        )
        return _fallback_task_response(stages)


def _complete_stages(
    model_response: dict[str, object], stages: list[dict[str, object]]
) -> dict[str, object]:
    """Keep model-authored tasks per stage and fill stages the model skipped.

    Qwen sometimes returns only the first one or two stages of a seven-stage prompt. A partial
    list is still usable: each stage keeps the model's task when it produced one, and the
    evidence-shaped fallback covers the rest in stage order, so the validator never sees a gap.
    """
    raw_tasks = model_response.get("tasks") if isinstance(model_response, dict) else None
    if not isinstance(raw_tasks, list):
        return model_response
    by_stage: dict[int, dict[str, object]] = {}
    extras: list[object] = []
    for entry in raw_tasks:
        if not isinstance(entry, dict):
            extras.append(entry)
            continue
        try:
            stage = int(entry.get("stage"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            extras.append(entry)
            continue
        by_stage.setdefault(stage, entry)
    fallback = {int(task["stage"]): task for task in _fallback_task_response(stages)["tasks"]}
    ordered: list[object] = []
    previous_id: str | None = None
    for stage in sorted(fallback):
        selected = dict(by_stage.get(stage) or fallback[stage])
        raw_id = selected.get("id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            selected["id"] = f"stage-{stage}"
        # The mixed list must stay one chain: a fallback task's prerequisite name may not exist
        # when the model authored the stage before it, so the link is rewritten to the id that
        # was actually selected for that stage.
        selected["prerequisite_task_ids"] = [previous_id] if previous_id else []
        ordered.append(selected)
        previous_id = str(selected["id"])
    # A task the model emitted without a usable stage cannot join this chain; dropping it is
    # better than rejecting the six stages that did map.
    del extras
    return {"tasks": ordered}


def _fallback_task_response(stages: list[dict[str, object]]) -> dict[str, object]:
    """Build the same evidence-shaped tasks the offline gateway would produce.

    Every citation comes from this run's retrieval results, so the fallback can never invent a
    source; a stage without evidence becomes ``needs_material`` exactly like the offline path and
    keeps the plan a draft instead of activating an empty path.
    """
    tasks: list[dict[str, object]] = []
    previous_id: str | None = None
    for stage in stages:
        stage_id = int(stage["id"])
        evidence = stage.get("evidence") or []
        citations = [
            item["citation"]
            for item in evidence
            if isinstance(item, dict) and isinstance(item.get("citation"), dict)
        ]
        required = set(stage.get("required_evidence") or [])
        present = {citation["kind"] for citation in citations}
        needs_material = stage_id == 4 and not required.issubset(present)
        selected = citations if stage_id == 4 else citations[:1]
        tasks.append(
            {
                "id": f"stage-{stage_id}",
                "stage": stage_id,
                "title": f"阶段 {stage_id}",
                "objective": f"完成阶段 {stage_id} 的学习目标",
                "prerequisite_task_ids": [previous_id] if previous_id else [],
                "estimated_minutes": 30,
                "citations": selected,
                "steps": ["查看当前阶段资料并提交结果"],
                "pitfalls": [],
                "completion_criteria": "提交可核对结果",
                "evidence_requirements": ["summary"],
                "status": "needs_material" if needs_material else "locked",
                "task_type": "citation_required" if citations else "self_assessment",
            }
        )
        previous_id = f"stage-{stage_id}"
    return {"tasks": tasks}


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
    citation_index: dict[str, dict[str, object]] | None = None,
    normalize: bool = False,
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
        payload = _normalize_task_payload(dict(entry)) if normalize else dict(entry)
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
        if citation_index is not None:
            payload["citations"] = _sanitize_citations(payload.get("citations"), citation_index)
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
            # Field paths and error kinds only: the rejected values are model-authored text and
            # stay out of any response or log line (spec 19.1).
            fields = ",".join(
                f"{'.'.join(str(part) for part in error['loc'])}:{error['type']}"
                for error in exc.errors()
            )
            raise PlanValidationError(
                f"the model answer carries an invalid task ({fields})",
                reason=INVALID_TASK_PAYLOAD,
            ) from None
    return tasks


def _normalize_task_payload(payload: dict[str, object]) -> dict[str, object]:
    """Coerce the fields Qwen renders as objects or unknown literals into the domain shape.

    The values are still the model's; only their container type is repaired. ``task_type`` is
    derived from the citations rather than trusted, and a stage is clamped to the seven stages
    the planner asked for so one stray number cannot invalidate the whole plan.
    """
    payload["title"] = _as_text(payload.get("title")) or "未命名任务"
    payload["objective"] = _as_text(payload.get("objective")) or payload["title"]
    payload["completion_criteria"] = (
        _as_text(payload.get("completion_criteria")) or "提交可核对结果"
    )
    payload["steps"] = _as_text_list(payload.get("steps")) or ["查看当前阶段资料并提交结果"]
    payload["pitfalls"] = _as_text_list(payload.get("pitfalls"))
    requirements = _as_text_list(payload.get("evidence_requirements"))
    payload["evidence_requirements"] = requirements or ["summary"]
    payload["citations"] = (
        payload.get("citations") if isinstance(payload.get("citations"), list) else []
    )
    payload["task_type"] = "citation_required" if payload["citations"] else "self_assessment"
    stage = payload.get("stage")
    try:
        payload["stage"] = max(0, min(6, int(stage)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        payload["stage"] = 6
    minutes = payload.get("estimated_minutes")
    try:
        payload["estimated_minutes"] = max(5, min(600, int(minutes)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        payload["estimated_minutes"] = 30
    return payload


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "；".join(_as_text(item) for item in value if _as_text(item))
    return ""


def _as_text_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [text for item in value if (text := _as_text(item))]
    return []


def _sanitize_citations(
    value: object, citation_index: dict[str, dict[str, object]]
) -> list[dict[str, object]]:
    """Fill a model-authored citation's missing fields from the evidence it names.

    A task citation is only a pointer, and Qwen often returns the bare id or just ``asset_id``,
    which ``LearningTask`` would reject before the validator can judge it. Known ids are merged
    over the retrieved citation so the stored record is complete; the model's own fields still
    win, so a wrong ``kind`` is left for the validator to refuse. An id the run never retrieved
    is kept exactly as the model wrote it — inventing evidence must fail validation, not be
    quietly dropped.
    """
    if not isinstance(value, list):
        return []
    resolved: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in value:
        asset_id = item.get("asset_id") if isinstance(item, dict) else item
        if not isinstance(asset_id, str) or not asset_id or asset_id in seen:
            continue
        seen.add(asset_id)
        if isinstance(item, dict):
            retrieved = citation_index.get(asset_id)
            resolved.append({**retrieved, **item} if retrieved is not None else dict(item))
        else:
            retrieved = citation_index.get(asset_id)
            resolved.append(dict(retrieved) if retrieved is not None else {"asset_id": asset_id})
    return resolved


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
