"""The gate every generated plan passes before a learner can act on it (FR-PLN-005..008).

Two answers come out of this module. A rejection is a :class:`PlanValidationError` — a
``ValueError`` so the service's existing error family keeps working, with a stable ``reason`` the
REST layer turns into ``422 plan_validation_failed`` without echoing the model's own text. A plan
that is *legal but thin* is not rejected at all: the task whose evidence is missing comes back
rewritten as ``needs_material``, which is what keeps the plan a draft (see
:mod:`labrelay.planning.generator`).

Ownership is decided from the project's own asset list, so an asset id that exists in another
project is not citable here, and a restricted or failed asset is not citable even inside its own
project: it was withheld from the index, so it is not evidence.
"""

from __future__ import annotations

from typing import Protocol

from labrelay.config import PlannerConfig
from labrelay.domain.models import Asset, LearningPlan, LearningTask
from labrelay.planning.stages import STAGE_REQUIRED_KINDS, stage_required_kinds

#: The stable ``reason`` codes. Tasks 11 and 14 branch on these, never on a message.
DUPLICATE_TASK_ID = "duplicate_task_id"
TASK_ID_TOO_LONG = "task_id_too_long"
UNKNOWN_STAGE = "unknown_stage"
STAGE_ORDER = "stage_order"
ENVIRONMENT_BEFORE_EXPERIMENTS = "environment_before_experiments"
UNKNOWN_PREREQUISITE = "unknown_prerequisite"
DEPENDENCY_CYCLE = "dependency_cycle"
PREREQUISITE_REQUIRED = "prerequisite_required"
UNKNOWN_ASSET = "unknown_asset"
RESTRICTED_ASSET = "restricted_asset"
CITATION_KIND_MISMATCH = "citation_kind_mismatch"

#: The stage whose evidence the spec pins exactly (FR-PLN-006): code, config and history.
BASELINE_STAGE = 4

ACTIVE_ASSET_STATUS = "active"

#: ``task_events``/``learning_task.id`` are VARCHAR(64) in ``src/labrelay/seekdb/schema.sql``, so
#: an identifier a model invented longer than that is refused here rather than at the store.
MAX_TASK_ID_LENGTH = 64


class PlanRepository(Protocol):
    """Only ``list_assets`` is needed here, kept structural so the port stays the port."""

    async def list_assets(self, project_id: str) -> list[Asset]: ...


class PlanValidationError(ValueError):
    """A plan that must not be saved or activated, and the one reason why.

    ``reason`` is the machine-readable half the API returns; the message is for logs and may name
    identifiers (an asset id, a task id) but never carries model-authored prose or a citation
    excerpt.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


async def validate_plan(
    repository: PlanRepository, config: PlannerConfig, plan: LearningPlan
) -> LearningPlan:
    """Check ``plan`` against this project's assets and return it with any material gaps marked."""
    assets = await repository.list_assets(plan.project_id)
    return validate_plan_sync(assets, config, plan)


def validate_plan_sync(
    assets: list[Asset], config: PlannerConfig, plan: LearningPlan
) -> LearningPlan:
    """The rule itself, with the asset rows handed in, so it is testable without a store."""
    _check_task_identity(plan)
    _check_stage_order(plan, config)
    _check_dependencies(plan.tasks)
    _check_activation_invariant(plan.tasks)

    assets_by_id = {asset.id: asset for asset in assets}
    for task in plan.tasks:
        _check_citations(task, plan, assets_by_id)

    return plan.model_copy(update={"tasks": [_mark_material_gaps(task) for task in plan.tasks]})


def _check_task_identity(plan: LearningPlan) -> None:
    for task in plan.tasks:
        if len(task.id) > MAX_TASK_ID_LENGTH:
            raise PlanValidationError(
                f"task id is longer than {MAX_TASK_ID_LENGTH} characters: {task.id[:24]}...",
                reason=TASK_ID_TOO_LONG,
            )
    task_ids = [task.id for task in plan.tasks]
    if len(task_ids) != len(set(task_ids)):
        raise PlanValidationError("plan task IDs must be unique", reason=DUPLICATE_TASK_ID)


def _check_stage_order(plan: LearningPlan, config: PlannerConfig) -> None:
    stages = [task.stage for task in plan.tasks]
    for stage in stages:
        if stage not in STAGE_REQUIRED_KINDS:
            raise PlanValidationError(
                f"plan {plan.id} uses an unknown stage: {stage}", reason=UNKNOWN_STAGE
            )
    if config.enforce_stage_order and stages != sorted(stages):
        raise PlanValidationError("plan stages must be non-decreasing", reason=STAGE_ORDER)
    if config.environment_before_experiments:
        has_environment = any(stage == 2 for stage in stages)
        has_experiment = any(stage >= BASELINE_STAGE for stage in stages)
        if has_experiment and not has_environment:
            raise PlanValidationError(
                "the environment stage must precede experiment stages",
                reason=ENVIRONMENT_BEFORE_EXPERIMENTS,
            )


def _check_dependencies(tasks: list[LearningTask]) -> None:
    by_id = {task.id: task for task in tasks}
    for task in tasks:
        for prerequisite in task.prerequisite_task_ids:
            if prerequisite not in by_id:
                raise PlanValidationError(
                    f"unknown prerequisite task: {prerequisite}", reason=UNKNOWN_PREREQUISITE
                )
        _detect_cycle(by_id, task.id, set(), set())


def _detect_cycle(
    by_id: dict[str, LearningTask],
    task_id: str,
    visiting: set[str],
    visited: set[str],
) -> None:
    if task_id in visiting:
        raise PlanValidationError("plan contains a dependency cycle", reason=DEPENDENCY_CYCLE)
    if task_id in visited:
        return
    visiting.add(task_id)
    for prerequisite in by_id[task_id].prerequisite_task_ids:
        _detect_cycle(by_id, prerequisite, visiting, visited)
    visiting.remove(task_id)
    visited.add(task_id)


def _check_activation_invariant(tasks: list[LearningTask]) -> None:
    """Every learner-facing task past stage 0 has to sit on something.

    The activation rule (see :mod:`labrelay.planning.generator`) opens a plan by turning the
    prerequisite-free roots ``locked -> todo``. A stage > 0 task with no prerequisite is therefore
    either an *immediate* day-one task the seven-stage path never intended (a learner handed the
    paper before the orientation) or a permanently unreachable one once the roots close — both
    corrupt the chain Task 11 unlocks step by step. Stage 0 is the only legal entry point, so a
    stage > 0 task must name at least one prerequisite.
    """
    for task in tasks:
        if task.stage > 0 and not task.prerequisite_task_ids:
            raise PlanValidationError(
                f"a stage {task.stage} task must name at least one prerequisite",
                reason=PREREQUISITE_REQUIRED,
            )


def _check_citations(
    task: LearningTask,
    plan: LearningPlan,
    assets_by_id: dict[str, Asset],
) -> None:
    # A ``citation_required`` task with no citation cannot be constructed at all — that rule lives
    # in :meth:`labrelay.domain.models.LearningTask.require_citation` and the generator reports it
    # as ``invalid_task_payload`` — so there is no missing-citation branch to take here. What is
    # left to check is whether each citation the task *does* carry resolves to this project's own
    # active evidence.
    #
    # ``config.every_task_requires_citation`` is deliberately not read here: the rule is enforced
    # unconditionally by the domain model above, so the setting documents that invariant rather
    # than toggling it (flipping it off would change nothing, and pretending otherwise would
    # misdescribe a real behaviour the validator does not own).
    for citation in task.citations:
        asset = assets_by_id.get(citation.asset_id)
        if asset is None or asset.project_id != plan.project_id:
            raise PlanValidationError(
                f"citation references unknown asset: {citation.asset_id}", reason=UNKNOWN_ASSET
            )
        if asset.status != ACTIVE_ASSET_STATUS:
            raise PlanValidationError(
                f"citation references unavailable asset: {citation.asset_id}",
                reason=RESTRICTED_ASSET,
            )
        if citation.kind != asset.kind:
            raise PlanValidationError(
                f"citation kind does not match asset: {citation.asset_id}",
                reason=CITATION_KIND_MISMATCH,
            )


def _mark_material_gaps(task: LearningTask) -> LearningTask:
    """A baseline that cannot cite code *and* config *and* history is a gap, not a to-do."""
    if task.stage != BASELINE_STAGE:
        return task
    # A finished task is a fact, not a proposal: Task 11 replans from the rows the learner already
    # completed, and rewriting ``done``/``skipped`` to ``needs_material`` would edit that history.
    if task.status in {"done", "skipped"}:
        return task
    required = set(stage_required_kinds(BASELINE_STAGE))
    present = {citation.kind for citation in task.citations}
    if required.issubset(present):
        return task
    return task.model_copy(update={"status": "needs_material"})
