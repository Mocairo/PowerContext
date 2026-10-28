"""Task 10: assessment scoring, evidence-backed plan generation and plan validation.

The three questions this file answers are the three the planner owns:

1. what does one learner answer set become (an ``Assessment`` with a server-minted id, filed in
   the repository and echoed to learner memory),
2. what may a plan be built from (only the citations this project's retrieval returned, plus the
   committed Handoff), and
3. when is a plan allowed to become executable (never while a task still says ``needs_material``,
   and never with a cycle, a stray prerequisite, an out-of-order stage, a foreign asset or a
   baseline that does not cite code *and* config *and* experiment).

Rejections are ``ValueError`` subclasses with a stable ``reason`` code, because the REST layer has
to name the gap without echoing model output.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from labrelay.application.services import LabRelayService
from labrelay.config import PlannerConfig
from labrelay.domain.assessment import Assessment, build_assessment
from labrelay.domain.models import LearningPlan, LearningTask, Project
from labrelay.importers.assets import import_directory
from labrelay.model.fake import DeterministicModelGateway
from labrelay.persistence.memory import InMemoryRepository
from labrelay.planning.generator import (
    HANDOFF_QUERY,
    PLANNING_STAGES,
    STAGE_EVIDENCE_LIMIT,
    stage_required_kinds,
)
from labrelay.planning.stages import STAGE_QUERY_PREFIX, STAGE_REQUIRED_KINDS
from labrelay.planning.validator import PlanValidationError, validate_plan_sync
from labrelay.retrieval.in_memory import InMemorySearchEngine
from tests.helpers_planning import (
    ASSET_KIND_BY_ID,
    FakeContext,
    advanced_assessment_payload,
    asset,
    baseline_task_payload,
    citation_payload,
    full_plan_tasks,
    make_assets,
    make_service,
    task_payload,
    valid_assessment_payload,
)

GENERATOR_LOGGER = "labrelay.planning.generator"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
PLANNER_CONFIG = PROJECT_ROOT / "configs" / "planner.yaml"
RETRIEVAL_CONFIG = PROJECT_ROOT / "configs" / "retrieval.yaml"

#: The stage the plan briefs query per stage; mirrored here so a drift is a failing test.
EXPECTED_STAGE_KINDS: dict[int, list[str]] = {
    0: ["operations", "note"],
    1: ["paper", "note"],
    2: ["code", "config", "operations"],
    3: ["code", "config"],
    4: ["code", "config", "experiment"],
    5: ["code", "experiment", "note"],
    6: ["note", "experiment"],
}


async def submit(service: Any) -> Assessment:
    return await service.submit_assessment("project-1", valid_assessment_payload())


def plan_response(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"tasks": tasks}


# --- assessment ----------------------------------------------------------------


async def test_submit_assessment_files_a_server_minted_id_and_remembers_the_goal() -> None:
    service, project = await make_service(assets=make_assets(), model_response={})
    context: Any = service.context

    assessment = await submit(service)

    assert assessment.id.startswith("assessment-")
    assert assessment.project_id == project.id
    stored = await service.repository.get_assessment(assessment.id)
    assert stored is not None
    assert stored["id"] == assessment.id
    assert stored["project_id"] == project.id
    assert stored["hours_per_week"] == 10
    # Round-tripping the stored row must give the same answers back (Task 11 replans from them).
    assert Assessment.model_validate(stored) == assessment
    scope_id, kind, text, reason = context.remembered[-1]
    assert (scope_id, kind) == (project.learner_scope_id, "preference")
    assert "reproduce_baseline" in text and "10" in text
    assert reason == f"assessment:{assessment.id}"


async def test_submit_assessment_refuses_a_forged_identity_in_the_payload() -> None:
    """The caller does not choose which row an assessment writes onto."""
    service, _ = await make_service(assets=make_assets(), model_response={})

    assessment = await service.submit_assessment(
        "project-1",
        {**valid_assessment_payload(), "id": "assessment-forged", "project_id": "project-evil"},
    )

    assert assessment.id != "assessment-forged"
    assert assessment.project_id == "project-1"
    assert await service.repository.get_assessment("assessment-forged") is None


@pytest.mark.parametrize(
    "field",
    ["python_level", "linux_server_level", "paper_level", "reproduction_level", "target"],
)
async def test_submit_assessment_rejects_an_unknown_level(field: str) -> None:
    service, _ = await make_service(assets=make_assets(), model_response={})

    with pytest.raises(ValidationError):
        await service.submit_assessment("project-1", {**valid_assessment_payload(), field: "guru"})


@pytest.mark.parametrize("hours", [0, 81])
async def test_submit_assessment_rejects_an_impossible_weekly_budget(hours: int) -> None:
    service, _ = await make_service(assets=make_assets(), model_response={})

    with pytest.raises(ValidationError):
        await service.submit_assessment(
            "project-1", {**valid_assessment_payload(), "hours_per_week": hours}
        )


async def test_submit_assessment_needs_an_existing_project() -> None:
    service, _ = await make_service(assets=make_assets(), model_response={})

    with pytest.raises(KeyError):
        await service.submit_assessment("project-none", valid_assessment_payload())


def test_assessment_scoring_shapes_the_plan_instead_of_ranking_the_learner() -> None:
    assessment = build_assessment(
        {
            "id": "assessment-1",
            "project_id": "project-1",
            "python_level": "none",
            "linux_server_level": "none",
            "paper_level": "read_related",
            "reproduction_level": "basic",
            "hours_per_week": 12,
            "target": "understand",
        }
    )

    assert assessment.needs_foundations is True
    assert assessment.needs_operations_training is True
    assert assessment.paper_depth == 45
    assert assessment.environment_minutes == 45
    # The scoring travels inside the dump the planner sends, so the model sees it too.
    assert assessment.model_dump(mode="json")["needs_foundations"] is True


def test_assessment_of_an_experienced_learner_asks_for_less_foundational_time() -> None:
    assessment = build_assessment(
        {
            "id": "assessment-2",
            "project_id": "project-1",
            **advanced_assessment_payload(),
            "paper_level": "detailed",
            "reproduction_level": "experienced",
        }
    )

    assert assessment.needs_foundations is False
    assert assessment.needs_operations_training is False
    assert assessment.paper_depth == 20
    assert assessment.environment_minutes == 20


# --- generation ----------------------------------------------------------------


async def test_generate_plan_activates_a_fully_evidenced_plan() -> None:
    service, project = await make_service(
        assets=make_assets(), model_response=plan_response(full_plan_tasks())
    )
    assessment = await submit(service)

    plan = await service.generate_plan(project.id, assessment.id)
    stored_project = await service.repository.get_project(project.id)

    assert plan.status == "active"
    assert plan.version == 1
    assert plan.assessment_id == assessment.id
    assert stored_project is not None and stored_project.active_plan_id == plan.id
    assert [task.stage for task in plan.tasks] == sorted(task.stage for task in plan.tasks)
    assert all(task.citations for task in plan.tasks)
    # Only the stage 0 roots become executable; the chain unlocks through Task 11's progress rules.
    # Every stage > 0 task carries a prerequisite (the activation invariant), so only orientation
    # is a prerequisite-free entry point and only it leaves ``locked`` on day one.
    assert [task.status for task in plan.tasks] == [
        "todo",
        "locked",
        "locked",
        "locked",
        "locked",
        "locked",
        "locked",
    ]
    assert [t.status for t in (await service.repository.get_plan(plan.id)).tasks][:1] == ["todo"]
    assert await service.repository.get_task(plan.tasks[3].id) == plan.tasks[3]
    assert service.context.remembered[-1][1] == "progress"
    assert service.context.remembered[-1][3] == f"plan:{plan.id}"


async def test_generation_reads_the_handoff_and_queries_evidence_per_stage() -> None:
    from tests.helpers_planning import FakeContext

    context = FakeContext(handoff_text="baseline 已完成，下一步是学习率消融")
    service, project = await make_service(
        assets=make_assets(),
        model_response=plan_response(full_plan_tasks()),
        context=context,
    )
    assessment = await submit(service)

    plan = await service.generate_plan(project.id, assessment.id)

    # The handoff is read from the project scope, and only from it.
    assert [call[0] for call in context.prepared] == [project.project_scope_id]
    assert context.prepared[0][1] == HANDOFF_QUERY
    model = service.model
    assert model.calls[0][1]["handoff"] == "baseline 已完成，下一步是学习率消融"
    assert [call[2] for call in service.engine.calls] == [
        EXPECTED_STAGE_KINDS[stage] for stage in range(7)
    ]
    assert all(
        call[0] == project.id and call[3] == STAGE_EVIDENCE_LIMIT for call in service.engine.calls
    )
    # The prompt carries the ids the model may cite and the plan those ids belong to.
    payload = model.calls[0][1]
    assert payload["assessment"]["id"] == assessment.id
    assert payload["plan_id"] == plan.id
    assert payload["plan_version"] == 1
    stage_payloads = payload["stages"]
    assert [entry["id"] for entry in stage_payloads] == list(range(7))
    assert [entry["required_evidence"] for entry in stage_payloads] == [
        EXPECTED_STAGE_KINDS[stage] for stage in range(7)
    ]
    assert {
        hit["citation"]["asset_id"] for entry in stage_payloads for hit in entry["evidence"]
    } <= set(ASSET_KIND_BY_ID)
    assert "asset_id" in model.calls[0][0]


async def test_generated_task_ids_are_namespaced_by_plan_id() -> None:
    """Carry-forward from Task 8: one assessment, two plan versions, two disjoint id spaces."""
    service, project = await make_service(assets=make_assets(), model=DeterministicModelGateway())
    assessment = await submit(service)

    first = await service.generate_plan(project.id, assessment.id)
    second = await service.generate_plan(project.id, assessment.id)

    assert (first.version, second.version) == (1, 2)
    assert {task.id for task in first.tasks}.isdisjoint({task.id for task in second.tasks})
    assert len({task.id for task in first.tasks + second.tasks}) == 14
    assert all(len(task.id) <= 64 for task in first.tasks + second.tasks)
    # v1 is still readable task by task: v2 did not overwrite it through a shared primary key.
    for task in first.tasks:
        assert (await service.repository.get_task(task.id)).plan_id == first.id
    stored = await service.repository.get_plan(second.id)
    assert stored is not None and [t.id for t in stored.tasks] == [t.id for t in second.tasks]


async def test_task_ids_are_namespaced_server_side_even_when_the_gateway_ignores_plan_id() -> None:
    """C1: the store's task ids never come from the gateway — the server rewrites them per plan.

    A gateway that ignores the ``plan_id`` it was handed (an older fake, a real model that only
    names tasks ``orientation``/``paper``) still produces plan-scoped, bounded ids: the
    :func:`_parse_tasks` mapping is the authority, not the model and not prompt compliance.
    """
    service, project = await make_service(assets=make_assets(), model_response={})
    service.model = LegacyStyleGateway()
    assessment = await submit(service)

    first = await service.generate_plan(project.id, assessment.id)
    second = await service.generate_plan(project.id, assessment.id)

    assert all(len(task.id) <= 64 for task in first.tasks + second.tasks)
    assert {task.id for task in first.tasks}.isdisjoint({task.id for task in second.tasks})
    assert all(task.plan_id == first.id for task in first.tasks)


NATURAL_IDS = ["orientation", "paper", "environment", "code", "baseline", "next", "summary"]


async def test_two_plans_from_a_raw_model_gateway_stay_disjoint_and_intact() -> None:
    """C1 regression: a raw model that returns natural ids must not collide across versions.

    Two consecutive plans for one project replay the *same* natural task ids (``orientation``,
    ``paper`` ...). Without server-side namespacing they would share ``learning_task.id`` primary
    keys and the second plan would overwrite the first's rows. They must instead land in disjoint
    id spaces with both versions readable back intact.
    """
    service, project = await make_service(
        assets=make_assets(), model_response=plan_response(full_plan_tasks())
    )
    assessment = await submit(service)

    first = await service.generate_plan(project.id, assessment.id)
    second = await service.generate_plan(project.id, assessment.id)

    assert {task.id for task in first.tasks}.isdisjoint({task.id for task in second.tasks})
    assert len({task.id for task in first.tasks + second.tasks}) == 14
    assert all(len(task.id) <= 64 for task in first.tasks + second.tasks)
    # the natural suffix survives inside the namespaced id
    assert [task.id.rsplit("-", 1)[-1] for task in first.tasks] == NATURAL_IDS
    # each plan's prerequisites resolve inside its own id space
    for plan in (first, second):
        own = {task.id for task in plan.tasks}
        for task in plan.tasks:
            assert set(task.prerequisite_task_ids) <= own
            assert task.plan_id == plan.id
    # get_plan and get_task both still return this version's rows
    for plan in (first, second):
        reread = await service.repository.get_plan(plan.id)
        assert [task.id for task in reread.tasks] == [task.id for task in plan.tasks]
        for task in plan.tasks:
            assert (await service.repository.get_task(task.id)).plan_id == plan.id


class LegacyStyleGateway:
    """A deterministic gateway that namespaces by assessment only, as Task 8 first shipped."""

    dimension = 2

    def __init__(self) -> None:
        self._inner = DeterministicModelGateway()

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._inner.embed(texts)

    async def generate_json(self, system_prompt, user_payload):
        stripped = {key: value for key, value in user_payload.items() if key != "plan_id"}
        return await self._inner.generate_json(system_prompt, stripped)


async def test_generation_over_the_real_engine_reaches_citations_into_the_plan(
    tmp_path: Path,
) -> None:
    """I1: the real InMemorySearchEngine + import_directory, not a FakeSearch double.

    Proves stage evidence and citations from actually-imported files reach the generated plan and
    resolve back to those assets, so the retrieval-backed guarantee holds at the real port boundary.
    """
    source = tmp_path / "handoff"
    source.mkdir()
    (source / "train.py").write_text("def train():\n    return run_baseline()\n", encoding="utf-8")
    (source / "config.yaml").write_text("learning_rate: 0.1\nepochs: 30\n", encoding="utf-8")
    (source / "experiment_results.csv").write_text("run,accuracy\n1,0.81\n", encoding="utf-8")
    (source / "handoff.md").write_text("项目目标 研究问题 核心方法 结论\n", encoding="utf-8")
    (source / "server_runbook.md").write_text("资源申请 资源释放 禁止事项\n", encoding="utf-8")

    repository = InMemoryRepository()
    await repository.initialize()
    project = Project(
        id="project-1",
        title="Aurora",
        research_area="DL",
        description="baseline",
        project_scope_id="project:project-1",
        learner_scope_id="learner:project-1",
    )
    await repository.save_project(project)

    gateway = DeterministicModelGateway()
    engine = InMemorySearchEngine(model=gateway)
    result = await import_directory(repository, engine, project.id, source)
    assert result["failed"] == 0
    # ``ImportResult.assets`` is a persisted *count*; the Asset records themselves live in the
    # repository, so the evidence assertions read them back from there.
    imported_assets = list(repository.assets.values())
    assert result["assets"] == len(imported_assets)
    imported_kinds = {record.kind for record in imported_assets}
    assert {"code", "config", "experiment"} <= imported_kinds

    service = LabRelayService(repository, engine, FakeContext(), gateway, PlannerConfig())
    assessment = await service.submit_assessment(project.id, valid_assessment_payload())
    plan = await service.generate_plan(project.id, assessment.id)

    active_asset_ids = {record.id for record in imported_assets if record.status == "active"}
    cited_ids = {citation.asset_id for task in plan.tasks for citation in task.citations}
    # Every citation the plan carries resolves to a real, active asset the importer persisted.
    assert cited_ids
    assert cited_ids <= active_asset_ids

    baseline = next(task for task in plan.tasks if task.stage == 4)
    cited_kinds = {citation.kind for citation in baseline.citations}
    assert {"code", "config", "experiment"} <= cited_kinds
    assert plan.status == "active"


# --- validation ----------------------------------------------------------------


async def run_plan(tasks: list[dict[str, Any]], **kwargs: Any):
    service, project = await make_service(
        assets=kwargs.pop("assets", make_assets()),
        model_response=plan_response(tasks),
        **kwargs,
    )
    assessment = await submit(service)
    return service, project, await service.generate_plan(project.id, assessment.id)


@pytest.mark.parametrize(
    ("tasks", "reason", "fragment"),
    [
        (
            [task_payload("a", 0, "ops-1", ("b",)), task_payload("b", 1, "note-1", ("a",))],
            "dependency_cycle",
            "dependency cycle",
        ),
        (
            [task_payload("a", 0, "ops-1", ("ghost",))],
            "unknown_prerequisite",
            "unknown prerequisite",
        ),
        (
            [task_payload("b", 1, "note-1"), task_payload("a", 0, "ops-1")],
            "stage_order",
            "non-decreasing",
        ),
        (
            [task_payload("a", 0, "ops-1"), task_payload("a", 1, "note-1")],
            "duplicate_task_id",
            "unique",
        ),
        (
            [task_payload("x", 9, "ops-1")],
            "unknown_stage",
            "unknown stage",
        ),
        (
            [
                task_payload("orientation", 0, "ops-1"),
                task_payload("code", 3, "code-1"),
                baseline_task_payload("baseline", ("code",)),
            ],
            "environment_before_experiments",
            "environment",
        ),
        (
            [task_payload("a", 0, "note-1", citations=[citation_payload("ghost-1", "note")])],
            "unknown_asset",
            "unknown asset",
        ),
        (
            [task_payload("a", 0, "ops-1", citations=[citation_payload("ops-1", "code")])],
            "citation_kind_mismatch",
            "kind",
        ),
    ],
    ids=[
        "cycle",
        "unknown_prerequisite",
        "stage_order",
        "duplicate_id",
        "unknown_stage",
        "missing_environment",
        "missing_asset",
        "kind_mismatch",
    ],
)
async def test_an_unacceptable_plan_is_rejected(tasks, reason: str, fragment: str) -> None:
    service, project = await make_service(assets=make_assets(), model_response=plan_response(tasks))
    assessment = await submit(service)

    with pytest.raises(PlanValidationError) as refused:
        await service.generate_plan(project.id, assessment.id)

    assert refused.value.reason == reason
    assert fragment in str(refused.value)
    assert isinstance(refused.value, ValueError)
    # Nothing half-validated is stored or pointed at.
    stored = await service.repository.get_project(project.id)
    assert stored is not None and stored.active_plan_id is None
    assert service.repository.plans == {}


async def test_a_non_root_stage_task_without_a_prerequisite_is_rejected() -> None:
    """I2: the activation invariant — only a stage 0 task may be a prerequisite-free root."""
    tasks = [task_payload("orientation", 0, "ops-1"), task_payload("orphan", 2, "config-1")]
    service, project = await make_service(assets=make_assets(), model_response=plan_response(tasks))
    assessment = await submit(service)

    with pytest.raises(PlanValidationError) as refused:
        await service.generate_plan(project.id, assessment.id)

    assert refused.value.reason == "prerequisite_required"
    assert service.repository.plans == {}
    stored = await service.repository.get_project(project.id)
    assert stored is not None and stored.active_plan_id is None


async def test_reactivating_supersedes_the_previous_active_plan() -> None:
    """Minor: a second generated plan must not leave two plans ``active`` for one project."""
    service, project = await make_service(assets=make_assets(), model=DeterministicModelGateway())
    assessment = await submit(service)

    first = await service.generate_plan(project.id, assessment.id)
    second = await service.generate_plan(project.id, assessment.id)

    assert (await service.repository.get_plan(first.id)).status == "superseded"
    assert (await service.repository.get_plan(second.id)).status == "active"
    active = [plan for plan in service.repository.plans.values() if plan.status == "active"]
    assert [plan.id for plan in active] == [second.id]
    stored = await service.repository.get_project(project.id)
    assert stored is not None and stored.active_plan_id == second.id


@pytest.mark.parametrize("minutes", [0, 5000])
async def test_malformed_estimated_minutes_is_rejected(minutes: int) -> None:
    """A domain bound (1..2880) keeps model noise out of the UI."""
    tasks = [task_payload("a", 0, "ops-1", estimated_minutes=minutes)]
    service, project = await make_service(assets=make_assets(), model_response=plan_response(tasks))
    assessment = await submit(service)

    with pytest.raises(PlanValidationError) as refused:
        await service.generate_plan(project.id, assessment.id)

    assert refused.value.reason == "invalid_task_payload"


@pytest.mark.parametrize("prerequisites", [123, True, None], ids=["number", "boolean", "null"])
async def test_a_non_sequence_prerequisite_list_is_an_invalid_payload(
    prerequisites: Any,
) -> None:
    """``prerequisite_task_ids`` that cannot be iterated is a 422 reason, never a 500.

    A number or boolean used to reach ``list()`` in ``_parse_tasks`` and raise ``TypeError``,
    which the API answers as a redacted 500; an explicit ``null`` was silently folded into "no
    prerequisites", which is a different answer than the model gave. Both are unreadable payloads.
    """
    tasks = [task_payload("a", 0, "ops-1", prerequisite_task_ids=prerequisites)]
    service, project = await make_service(assets=make_assets(), model_response=plan_response(tasks))
    assessment = await submit(service)

    with pytest.raises(PlanValidationError) as refused:
        await service.generate_plan(project.id, assessment.id)

    assert refused.value.reason == "invalid_task_payload"


async def test_a_missing_prerequisite_key_still_defaults_to_no_prerequisites() -> None:
    """Absent is not malformed: the domain default (no prerequisites) keeps working."""
    task = task_payload("a", 0, "ops-1")
    del task["prerequisite_task_ids"]
    service, project = await make_service(
        assets=make_assets(), model_response=plan_response([task])
    )
    assessment = await submit(service)

    plan = await service.generate_plan(project.id, assessment.id)

    assert plan.tasks[0].prerequisite_task_ids == []


async def test_stage_order_rule_can_be_disabled_by_planner_config() -> None:
    """Minor: ``enforce_stage_order`` is read from PlannerConfig, not ignored."""
    tasks = [
        task_payload("later", 1, "note-1", ("root",)),
        task_payload("root", 0, "ops-1"),
    ]

    # Default config enforces the ordering, so the descending plan is rejected.
    enforcing, project = await make_service(
        assets=make_assets(), model_response=plan_response(tasks)
    )
    assessment = await enforcing.submit_assessment(project.id, valid_assessment_payload())
    with pytest.raises(PlanValidationError) as refused:
        await enforcing.generate_plan(project.id, assessment.id)
    assert refused.value.reason == "stage_order"

    # With the rule switched off the same plan validates and activates.
    relaxed, project = await make_service(
        assets=make_assets(),
        model_response=plan_response(tasks),
        planner_config=PlannerConfig(enforce_stage_order=False),
    )
    assessment = await relaxed.submit_assessment(project.id, valid_assessment_payload())
    plan = await relaxed.generate_plan(project.id, assessment.id)
    assert plan.status == "active"


async def test_a_citation_of_another_projects_asset_is_rejected() -> None:
    """Ownership is decided by the project's own asset list, never by a globally unique id."""
    tasks = [task_payload("a", 0, "note-1", citations=[citation_payload("code-evil", "code")])]
    service, project = await make_service(
        assets=[*make_assets(), asset("code-evil", "code", project_id="project-evil")],
        model_response=plan_response(tasks),
    )
    assessment = await submit(service)

    with pytest.raises(PlanValidationError) as refused:
        await service.generate_plan(project.id, assessment.id)

    assert refused.value.reason == "unknown_asset"
    assert "code-evil" in str(refused.value)


async def test_a_citation_of_a_restricted_asset_is_rejected() -> None:
    """Evidence withheld from the index is not evidence, even when its id really exists."""
    restricted = asset("note-secret", "note").model_copy(update={"status": "restricted"})
    tasks = [task_payload("a", 0, "note-1", citations=[citation_payload("note-secret", "note")])]
    service, project = await make_service(
        assets=[*make_assets(), restricted],
        model_response=plan_response(tasks),
    )
    assessment = await submit(service)

    with pytest.raises(PlanValidationError) as refused:
        await service.generate_plan(project.id, assessment.id)

    assert refused.value.reason == "restricted_asset"
    assert "note-secret" in str(refused.value)


async def test_a_task_json_the_domain_refuses_is_reported_as_an_invalid_payload() -> None:
    """A citation_required task with no citations cannot even become a ``LearningTask``."""
    service, project = await make_service(
        assets=make_assets(),
        model_response=plan_response([task_payload("a", 0, "ops-1", citations=[])]),
    )
    assessment = await submit(service)

    with pytest.raises(PlanValidationError) as refused:
        await service.generate_plan(project.id, assessment.id)

    assert refused.value.reason == "invalid_task_payload"


async def test_a_model_answer_without_tasks_is_an_invalid_payload() -> None:
    service, project = await make_service(assets=make_assets(), model_response={"steps": []})
    assessment = await submit(service)

    with pytest.raises(PlanValidationError) as refused:
        await service.generate_plan(project.id, assessment.id)

    assert refused.value.reason == "invalid_task_payload"


async def test_baseline_without_config_and_experiment_stays_a_needs_material_draft() -> None:
    """Brief test 3: the gap is a material gap, not a crash and not an executable task."""
    tasks = [
        task_payload("orientation", 0, "ops-1"),
        task_payload("environment", 2, "config-1", ("orientation",)),
        task_payload("baseline", 4, "code-1", ("environment",)),
    ]
    service, project = await make_service(assets=make_assets(), model_response=plan_response(tasks))
    assessment = await submit(service)

    plan = await service.generate_plan(project.id, assessment.id)
    stored_project = await service.repository.get_project(project.id)

    assert stored_project is not None and stored_project.active_plan_id is None
    assert plan.status == "draft"
    assert plan.tasks[-1].status == "needs_material"
    # A draft keeps its locked root: nothing is executable until the material arrives.
    assert [task.status for task in plan.tasks] == ["locked", "locked", "needs_material"]
    assert (await service.repository.get_plan(plan.id)).status == "draft"
    assert await service.repository.get_active_plan(project.id) is None


async def test_a_model_answer_may_flag_its_own_gap_but_never_its_own_progress() -> None:
    """Review I3: a model may admit it lacks evidence; it may not declare its own progress.

    ``done``/``skipped`` in the answer would be history no learner made: no ``evidence_submitted``
    event behind it, a completion count already filled in, and a successor chain open on day one.
    Those statuses are ignored and the domain default stands, while ``needs_material`` stays
    honoured -- that one is the model reporting a gap, which the validator also re-checks.
    """
    service, project, plan = await run_plan(
        [
            task_payload("orientation", 0, "ops-1", status="done"),
            task_payload("paper", 1, "note-1", ("orientation",), status="skipped"),
            task_payload("lab", 2, "config-1", ("paper",), status="in_progress"),
        ]
    )

    assert [task.status for task in plan.tasks] == ["todo", "locked", "locked"]
    assert await service.repository.get_active_plan(project.id) is not None
    assert await service.repository.list_events(project.id) == []


async def test_a_fabricated_done_baseline_cannot_bypass_the_stage_four_evidence_rule() -> None:
    """The same fabricated status on a stage-4 task is still a material gap, not a completion."""
    service, _, plan = await run_plan(
        [
            task_payload("orientation", 0, "ops-1", status="done"),
            task_payload("environment", 2, "config-1", ("orientation",)),
            task_payload("baseline", 4, "code-1", ("environment",), status="done"),
        ]
    )

    assert plan.status == "draft"
    assert plan.tasks[-1].status == "needs_material"


def test_the_material_gap_rule_exempts_only_carried_history() -> None:
    """``source_task_id`` is what makes a finished row a fact; a claimed status is not."""
    fresh = LearningTask.model_validate(
        {
            **task_payload("baseline-fresh", 4, "code-1", ("orientation",)),
            "project_id": "project-1",
            "plan_id": "plan-x",
            "status": "done",
        }
    )
    carried = LearningTask.model_validate(
        {
            **task_payload("baseline-carried", 4, "code-1", ("orientation",)),
            "project_id": "project-1",
            "plan_id": "plan-x",
            "status": "done",
            "source_task_id": "old-baseline",
        }
    )
    root = LearningTask.model_validate(
        {
            **task_payload("orientation", 0, "ops-1"),
            "project_id": "project-1",
            "plan_id": "plan-x",
        }
    )
    plan = LearningPlan(
        id="plan-x",
        project_id="project-1",
        version=2,
        status="draft",
        assessment_id="assessment-1",
        tasks=[root, fresh, carried],
    )

    checked = validate_plan_sync(
        make_assets(),
        PlannerConfig(environment_before_experiments=False),
        plan,
    )

    assert [task.status for task in checked.tasks] == ["locked", "needs_material", "done"]


async def test_a_material_gap_is_logged_with_a_stable_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=GENERATOR_LOGGER):
        service, _, plan = await run_plan(
            [
                task_payload("orientation", 0, "ops-1"),
                task_payload("environment", 2, "config-1", ("orientation",)),
                task_payload("baseline", 4, "code-1", ("environment",)),
            ]
        )

    assert "plan_not_activated" in caplog.text
    assert "reason=insufficient_cited_evidence" in caplog.text
    # The logged task id is the server-namespaced one, not the model's raw ``baseline`` token.
    assert f"task_ids={plan.tasks[-1].id}" in caplog.text
    assert plan.tasks[-1].id.rsplit("-", 1)[-1] == "baseline"
    assert f"plan_id={plan.id}" in caplog.text
    # A new session recovers the gap from learner memory, not from the log.
    gap_notes = [note for note in service.context.remembered if "资料不足" in note[2]]
    assert [(note[0], note[1], note[3]) for note in gap_notes] == [
        ("learner:project-1", "progress", f"plan:{plan.id}:needs_material")
    ]
    assert plan.tasks[-1].title in gap_notes[0][2]


async def test_a_self_assessment_task_without_evidence_is_not_required_to_cite() -> None:
    """FR-PLN-005 exempts pure self-assessment tasks from the citation rule."""
    tasks = [
        task_payload("orientation", 0, "ops-1"),
        task_payload(
            "checkin", 1, "note-1", ("orientation",), task_type="self_assessment", citations=[]
        ),
    ]
    _, project, plan = await run_plan(tasks)

    assert plan.status == "active"
    self_assessment = [task for task in plan.tasks if task.task_type == "self_assessment"]
    assert [task.id.rsplit("-", 1)[-1] for task in self_assessment] == ["checkin"]
    assert project.id == "project-1"


async def test_stage_required_kinds_matches_the_plan_and_rejects_an_unknown_stage() -> None:
    assert [stage_required_kinds(stage) for stage in range(7)] == [
        EXPECTED_STAGE_KINDS[stage] for stage in range(7)
    ]
    with pytest.raises(KeyError):
        stage_required_kinds(7)


async def test_a_generated_task_keeps_the_plan_and_project_it_belongs_to() -> None:
    """Model output cannot rehome itself into another project or plan."""
    tasks = [task_payload("a", 0, "ops-1", project_id="project-evil", plan_id="plan-evil")]
    _, _, plan = await run_plan(tasks)

    assert plan.tasks[0].project_id == "project-1"
    assert plan.tasks[0].plan_id == plan.id
    assert isinstance(plan.tasks[0], LearningTask)


async def test_long_model_ids_are_capped_into_the_identifier_column() -> None:
    """C1: over-long model ids are folded into VARCHAR(64), kept distinct and plan-scoped."""
    long_a = "task-" + "x" * 70
    long_b = "task-" + "x" * 70 + "tail"  # same head, differs only past the truncation point
    tasks = [task_payload(long_a, 0, "ops-1"), task_payload(long_b, 0, "note-1")]
    _, _, plan = await run_plan(tasks)

    stored = [task.id for task in plan.tasks]
    assert all(len(task_id) <= 64 for task_id in stored)
    assert len(set(stored)) == 2


def test_a_stored_id_wider_than_the_column_is_refused_by_the_validator() -> None:
    """The width guard still backstops a plan not built through the generator's mapper (Task 11,
    a store that hands a row back unchanged)."""
    from labrelay.domain.models import LearningPlan
    from labrelay.planning.validator import validate_plan_sync

    task = LearningTask.model_validate(
        {
            **task_payload("t" * 65, 0, "ops-1"),
            "project_id": "project-1",
            "plan_id": "plan-1",
        }
    )
    plan = LearningPlan(
        id="plan-1",
        project_id="project-1",
        version=1,
        status="draft",
        assessment_id="assessment-1",
        tasks=[task],
    )

    with pytest.raises(PlanValidationError) as refused:
        validate_plan_sync(make_assets(), PlannerConfig(), plan)

    assert refused.value.reason == "task_id_too_long"


async def test_an_empty_plan_is_rejected_rather_than_activated() -> None:
    """A plan with no tasks would activate, point the project at nothing and show a blank path."""
    service, project = await make_service(assets=make_assets(), model_response={"tasks": []})
    assessment = await submit(service)

    with pytest.raises(PlanValidationError) as refused:
        await service.generate_plan(project.id, assessment.id)

    assert refused.value.reason == "invalid_task_payload"
    assert service.repository.plans == {}


def test_the_stage_table_and_its_budget_match_the_shipped_config() -> None:
    """Same convention as Tasks 3, 4 and 6: the constants are pinned to the YAML they read."""
    planning = yaml.safe_load(PLANNER_CONFIG.read_text(encoding="utf-8"))["planning"]
    configured = {entry["id"]: entry["required_evidence"] for entry in planning["default_stages"]}
    constraints = planning["constraints"]
    retrieval = yaml.safe_load(RETRIEVAL_CONFIG.read_text(encoding="utf-8"))

    assert sorted(configured) == list(PLANNING_STAGES)
    assert set(STAGE_REQUIRED_KINDS) == set(STAGE_QUERY_PREFIX)
    for stage, kinds in configured.items():
        # The config lists what a stage is about; the planner requires at least that much.
        assert set(kinds) <= set(stage_required_kinds(stage))
    assert STAGE_EVIDENCE_LIMIT == retrieval["retrieval"]["fusion"]["output_limit"]
    assert PlannerConfig().enforce_stage_order is constraints["enforce_stage_order"]
    assert (
        PlannerConfig().environment_before_experiments
        is constraints["environment_before_experiments"]
    )
    assert (
        PlannerConfig().every_task_requires_citation is constraints["every_task_requires_citation"]
    )
    assert constraints["missing_evidence_status"] == "needs_material"
