"""Task 11: progress tracking, replanning, cross-session context recovery and reverse handoff.

Every test runs on the real :class:`InMemoryRepository` — the store the API hits until Task 12
wires the seekdb one — with the Task 10 planning doubles for memory, retrieval and the model. What
is pinned down here is the part of the product the learner actually touches:

* a completion is only accepted with the evidence the task declared, and it unlocks exactly the
  successors whose prerequisites are all done or skipped (spec 16.1),
* a skip is never silent: a machine-readable reason plus an assessment or a user confirmation
  source has to land in the append-only event (FR-PRG-008),
* replanning mints a new immutable plan version and leaves the previous plan, its tasks and every
  TaskEvent untouched (FR-PLN-009, FR-PRG-006),
* progress survives a memory outage (spec 20),
* a new session recovers the stage, the next task, what is finished and what is blocked
  (FR-PRG-005),
* the reverse Handoff is assembled from those durable events and committed through Task 9's service.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from powercontext.http import ArtifactReference, HandoffContent, PreparedHandoff

from labrelay.api.errors import REQUEST_ID_HEADER
from labrelay.api.main import create_app_with_service
from labrelay.api.schemas import MAX_EVIDENCE_BYTES, MAX_EVIDENCE_KEYS
from labrelay.application.handoff import HandoffDraft, HandoffService
from labrelay.application.services import IllegalTaskTransition, LabRelayService
from labrelay.config import PlannerConfig
from labrelay.context.powercontext import PowerContextUnavailableError
from labrelay.domain.models import LearningPlan, LearningTask, Project
from labrelay.learning.progress import (
    MAX_HANDOFF_CITATIONS,
    MISSING_EVIDENCE,
    PREREQUISITES_INCOMPLETE,
    SKIP_REQUIRES_CONFIRMATION,
    STALE_PLAN,
    TaskTransitionRejected,
)
from labrelay.persistence.memory import InMemoryRepository
from tests.helpers_planning import (
    PROJECT_ID,
    FakeContext,
    FakeSearch,
    ModelReturning,
    citation_payload,
    make_assets,
    task_payload,
    valid_assessment_payload,
)

PLAN_ID = "plan-1"
ASSESSMENT_ID = "assessment-1"
SERVICE_LOGGER = "labrelay.application.services"
ROUTES_LOGGER = "labrelay.api.routes"
PROGRESS_LOGGER = "labrelay.learning.progress"
EVIDENCE = {"summary": "已复述规则"}


class ExplodingContext(FakeContext):
    """Learner memory is unreachable, exactly as in Task 5's outage fixtures."""

    message = "powercontext call failed api_key=super-secret-value"

    async def remember(self, scope_id, kind, text, reason=None):
        raise RuntimeError(self.message)


class DownReadContext(FakeContext):
    """The learner scope answers the way a down PowerContext does."""

    async def prepare_context(self, scope_id: str, query: str, max_bytes: int):
        raise PowerContextUnavailableError("powercontext is down")


def make_task(
    task_id: str,
    stage: int,
    prereqs: tuple[str, ...] = (),
    *,
    asset_id: str | None = None,
    **overrides: Any,
) -> LearningTask:
    """One task row in the seeded plan, cited the way Task 10's generator would cite it."""
    payload = task_payload(
        task_id, stage, asset_id or ("ops-1" if stage == 0 else "note-1"), prereqs
    )
    status = overrides.pop("status", "locked")
    payload.update(overrides)
    return LearningTask.model_validate(
        {**payload, "project_id": PROJECT_ID, "plan_id": PLAN_ID, "status": status}
    )


def make_plan(*tasks: LearningTask) -> LearningPlan:
    return LearningPlan(
        id=PLAN_ID,
        project_id=PROJECT_ID,
        version=1,
        status="active",
        assessment_id=ASSESSMENT_ID,
        tasks=list(tasks),
    )


def replan_response() -> dict[str, Any]:
    """A two-task remainder that continues from the stage-0 task by its visible id."""
    return {
        "tasks": [
            task_payload("checkpoint", 1, "note-1", ("task-1",), title="核对缓存清理步骤"),
            task_payload("recovery", 2, "code-1", ("checkpoint",), title="恢复一次中断训练"),
        ]
    }


def bad_citation_response() -> dict[str, Any]:
    """A remainder that cites an asset this project does not hold."""
    return {
        "tasks": [
            task_payload("ghost", 0, "ops-1", citations=[citation_payload("ghost-asset", "note")]),
        ]
    }


async def build_service(
    *,
    context: FakeContext | None = None,
    model: Any | None = None,
    tasks: list[LearningTask] | None = None,
    seed_plan: bool = True,
) -> LabRelayService:
    """A project whose active plan is a three-stage chain, the way Task 10 leaves it."""
    repository = InMemoryRepository()
    project = Project(
        id=PROJECT_ID,
        title="Aurora",
        research_area="DL",
        description="baseline",
        project_scope_id=f"project:{PROJECT_ID}",
        learner_scope_id=f"learner:{PROJECT_ID}",
        active_plan_id=PLAN_ID if seed_plan else None,
    )
    await repository.save_project(project)
    for item in make_assets():
        await repository.save_asset(item)
    await repository.save_assessment(
        ASSESSMENT_ID, PROJECT_ID, dict(valid_assessment_payload(), id=ASSESSMENT_ID)
    )
    if seed_plan:
        chain = tasks or [
            make_task("task-1", 0, (), status="todo"),
            make_task("task-2", 1, ("task-1",)),
            make_task("task-3", 2, ("task-2",)),
        ]
        await repository.save_plan(make_plan(*chain))
    return LabRelayService(
        repository,
        FakeSearch(make_assets()),
        context if context is not None else FakeContext(),
        model if model is not None else ModelReturning(replan_response()),
        PlannerConfig(),
    )


async def complete_first(service: LabRelayService) -> LearningTask:
    """Drive the stage-0 task the way the UI does: start it, then finish it with evidence."""
    await service.update_task("task-1", "in_progress")
    return await service.update_task("task-1", "done", dict(EVIDENCE))


def client_for(service: LabRelayService, handoff: HandoffService | None = None) -> Any:
    app = create_app_with_service(service, handoff)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# --- progress on the service ----------------------------------------------------


async def test_a_completion_records_evidence_unlocks_the_successor_and_syncs_memory() -> None:
    context = FakeContext()
    service = await build_service(context=context)

    await service.update_task("task-1", "in_progress")
    done = await service.update_task("task-1", "done", dict(EVIDENCE))

    assert done.status == "done"
    assert (await service.repository.get_task("task-2")).status == "todo"
    assert (await service.repository.get_task("task-3")).status == "locked"

    events = await service.repository.list_events(PROJECT_ID)
    assert [event.event_type for event in events] == [
        "status_changed",
        "status_changed",
        "evidence_submitted",
        "task_unlocked",
    ]
    assert events[2].task_id == "task-1"
    assert events[2].payload["evidence"] == EVIDENCE
    assert events[3].payload == {"task_id": "task-2", "unlocked_by": "task-1"}

    notes = [note for _, _, note, _ in context.remembered]
    assert any("已完成" in note and done.title in note for note in notes)


@pytest.mark.parametrize("evidence", [None, {}, {"metrics": "0.91"}])
async def test_a_completion_without_its_declared_evidence_is_refused(
    evidence: dict[str, Any] | None,
) -> None:
    service = await build_service(tasks=[make_task("task-1", 0, (), status="in_progress")])

    with pytest.raises(TaskTransitionRejected) as failure:
        await service.update_task("task-1", "done", evidence)

    assert failure.value.reason == MISSING_EVIDENCE
    assert "summary" in str(failure.value)
    assert (await service.repository.get_task("task-1")).status == "in_progress"
    assert await service.repository.list_events(PROJECT_ID) == []


async def test_a_locked_task_stays_locked_until_every_prerequisite_is_finished() -> None:
    service = await build_service()

    with pytest.raises(TaskTransitionRejected) as failure:
        await service.update_task("task-2", "todo")

    assert failure.value.reason == PREREQUISITES_INCOMPLETE
    assert (await service.repository.get_task("task-2")).status == "locked"

    await complete_first(service)
    assert (await service.repository.get_task("task-2")).status == "todo"


async def test_completing_a_task_does_not_unlock_the_whole_chain_at_once() -> None:
    """One completion unlocks its own successors; ``todo`` is not a finished prerequisite."""
    service = await build_service()

    await complete_first(service)

    assert (await service.repository.get_task("task-3")).status == "locked"


@pytest.mark.parametrize(
    ("evidence", "accepted"),
    [
        (None, False),
        ({"reason": "已掌握"}, False),
        ({"skip_confirmed_by": "learner"}, False),
        ({"reason": "已掌握", "skip_confirmed_by": "learner"}, True),
        ({"reason": "测评通过", "assessment_id": ASSESSMENT_ID}, True),
    ],
)
async def test_a_skip_keeps_a_reason_and_its_confirmation_source(
    evidence: dict[str, Any] | None, accepted: bool
) -> None:
    service = await build_service(tasks=[make_task("task-1", 0, (), status="todo")])

    if not accepted:
        with pytest.raises(TaskTransitionRejected) as failure:
            await service.update_task("task-1", "skipped", evidence)
        assert failure.value.reason == SKIP_REQUIRES_CONFIRMATION
        assert (await service.repository.get_task("task-1")).status == "todo"
        assert await service.repository.list_events(PROJECT_ID) == []
        return

    skipped = await service.update_task("task-1", "skipped", evidence)

    assert skipped.status == "skipped"
    event = (await service.repository.list_events(PROJECT_ID))[0]
    assert event.payload["to"] == "skipped"
    assert event.payload["reason"] == evidence["reason"]  # type: ignore[index]
    assert set(evidence) <= set(event.payload)


async def test_a_skipped_prerequisite_unlocks_its_successor() -> None:
    service = await build_service()

    await service.update_task(
        "task-1", "skipped", {"reason": "已有基础", "skip_confirmed_by": "learner"}
    )

    assert (await service.repository.get_task("task-2")).status == "todo"


async def test_a_blocked_task_stores_the_reason_in_its_event() -> None:
    service = await build_service(tasks=[make_task("task-1", 0, (), status="in_progress")])

    blocked = await service.update_task("task-1", "blocked", {"reason": "缺 GPU"})

    assert blocked.status == "blocked"
    event = (await service.repository.list_events(PROJECT_ID))[0]
    assert event.event_type == "status_changed"
    assert event.payload == {"from": "in_progress", "to": "blocked", "reason": "缺 GPU"}


async def test_a_finished_task_cannot_be_reopened() -> None:
    service = await build_service()
    await complete_first(service)

    with pytest.raises(ValueError, match="Cannot transition"):
        await service.update_task("task-1", "todo")


async def test_an_unknown_task_raises_key_error_without_writing_anything() -> None:
    service = await build_service()

    with pytest.raises(KeyError):
        await service.update_task("task-none", "done", dict(EVIDENCE))

    assert await service.repository.list_events(PROJECT_ID) == []


async def test_a_memory_outage_keeps_the_progress_the_evidence_and_the_unlock(caplog) -> None:
    """Spec 20: the transition, its evidence and the unlocked successor all survive the outage."""
    service = await build_service(context=ExplodingContext())

    with caplog.at_level(logging.WARNING, logger=SERVICE_LOGGER):
        done = await complete_first(service)

    events = await service.repository.list_events(PROJECT_ID)
    types = [event.event_type for event in events]
    assert done.status == "done"
    assert (await service.repository.get_task("task-2")).status == "todo"
    # The whole trail of one completion, with the memory failure between the transition that
    # survived it and the evidence that was recorded anyway.
    assert types == [
        "status_changed",
        "status_changed",
        "memory_sync_failed",
        "evidence_submitted",
        "task_unlocked",
    ]
    assert events[2].payload == {"task_id": "task-1", "exception_class": "RuntimeError"}
    assert "memory sync failed" in caplog.text
    assert "super-secret-value" not in caplog.text
    assert "super-secret-value" not in str(events)


# --- replanning -----------------------------------------------------------------


async def test_replanning_creates_a_new_version_and_keeps_the_history_readable() -> None:
    service = await build_service()
    await complete_first(service)
    before = await service.repository.list_events(PROJECT_ID)
    old = await service.repository.get_plan(PLAN_ID)

    plan = await service.replan_project(PROJECT_ID, "检查点失败，需要补排障任务")

    assert plan.version == 2
    assert plan.status == "active"
    assert plan.id != PLAN_ID
    assert plan.assessment_id == ASSESSMENT_ID
    assert plan.tasks[0].status == "done"
    assert plan.tasks[0].source_task_id == "task-1"
    assert plan.tasks[0].citations == old.tasks[0].citations
    assert [task.status for task in plan.tasks[1:]] == ["todo", "locked"]
    # A new version never writes onto the previous one's rows.
    assert {task.id for task in plan.tasks}.isdisjoint({task.id for task in old.tasks})

    superseded = await service.repository.get_plan(PLAN_ID)
    assert superseded is not None and superseded.status == "superseded"
    # The source version keeps exactly the state the learner left it in, unlock included.
    assert [task.status for task in superseded.tasks] == ["done", "todo", "locked"]
    assert await service.repository.list_events(PROJECT_ID) == before
    assert (await service.repository.get_task("task-1")).plan_id == PLAN_ID
    assert (await service.repository.get_project(PROJECT_ID)).active_plan_id == plan.id

    stored = await service.repository.get_plan(plan.id)
    assert stored is not None
    assert [task.id for task in stored.tasks] == [task.id for task in plan.tasks]
    # The finished task's evidence stays reachable through the events it was recorded in.
    submitted = [event for event in before if event.event_type == "evidence_submitted"]
    assert submitted and submitted[0].payload["evidence"] == EVIDENCE


async def test_replanning_maps_the_model_onto_the_completed_task_by_its_visible_id() -> None:
    service = await build_service()
    await complete_first(service)

    plan = await service.replan_project(PROJECT_ID, "补充排障任务")

    assert plan.tasks[1].prerequisite_task_ids == [plan.tasks[0].id]


async def test_a_replan_that_fails_validation_leaves_the_active_plan_intact() -> None:
    service = await build_service(model=ModelReturning(bad_citation_response()))
    before = await service.repository.list_events(PROJECT_ID)

    with pytest.raises(ValueError):
        await service.replan_project(PROJECT_ID, "随便改改")

    assert (await service.repository.get_active_plan(PROJECT_ID)).id == PLAN_ID
    assert set(service.repository.plans) == {PLAN_ID}
    assert (await service.repository.get_project(PROJECT_ID)).active_plan_id == PLAN_ID
    assert await service.repository.list_events(PROJECT_ID) == before


async def test_a_completed_baseline_stays_completed_when_replanned() -> None:
    """A finished stage-4 task is history, not a proposal: the gap marker must not re-judge it."""
    service = await build_service(
        tasks=[
            make_task("task-1", 0, (), status="in_progress"),
            make_task("task-2", 2, ("task-1",), asset_id="config-1"),
            make_task(
                "task-4",
                4,
                ("task-2",),
                asset_id="code-1",
                evidence_requirements=["command_summary", "metrics_comparison"],
            ),
        ],
        model=ModelReturning({"tasks": [task_payload("analysis", 5, "note-1", ("task-4",))]}),
    )
    # Walk the chain the way the learner does, so the baseline really is finished work.
    await service.update_task("task-1", "done", dict(EVIDENCE))
    await service.update_task("task-2", "in_progress")
    await service.update_task("task-2", "done", {"summary": "环境可用"})
    await service.update_task("task-4", "in_progress")
    await service.update_task(
        "task-4",
        "done",
        {"command_summary": "python train.py", "metrics_comparison": "0.82 vs 0.80"},
    )

    plan = await service.replan_project(PROJECT_ID, "指标偏差需复查")

    carried = [task for task in plan.tasks if task.source_task_id == "task-4"]
    assert [task.status for task in carried] == ["done"]
    # The original row and its evidence are still the record, whatever the new version says.
    assert (await service.repository.get_task("task-4")).status == "done"
    assert plan.tasks[-1].prerequisite_task_ids == [carried[0].id]


async def test_a_replan_that_re_proposes_finished_work_drops_the_duplicate() -> None:
    """A model that re-lists a completed task cannot un-finish it or double its row."""
    service = await build_service(
        model=ModelReturning(
            {
                "tasks": [
                    task_payload("task-1", 0, "ops-1"),
                    task_payload("checkpoint", 1, "note-1", ("task-1",)),
                ]
            }
        )
    )
    await complete_first(service)

    plan = await service.replan_project(PROJECT_ID, "沿用旧任务名")

    assert len(plan.tasks) == 2
    assert plan.tasks[0].source_task_id == "task-1"
    assert plan.tasks[0].status == "done"
    assert plan.tasks[1].prerequisite_task_ids == [plan.tasks[0].id]


async def test_replanning_without_an_active_plan_is_refused() -> None:
    service = await build_service(seed_plan=False)

    with pytest.raises(ValueError, match="no active plan"):
        await service.replan_project(PROJECT_ID, "没有路径可调整")

    assert service.repository.plans == {}


async def test_replanning_records_the_recovered_position_in_learner_memory() -> None:
    context = FakeContext()
    service = await build_service(context=context)
    await complete_first(service)
    context.remembered.clear()

    await service.replan_project(PROJECT_ID, "资料缺失，需要调整")

    notes = [note for _, _, note, _ in context.remembered]
    assert any("重规划" in note and "已完成 1" in note for note in notes)
    assert any("核对缓存清理步骤" in note for note in notes)


# --- learning context -----------------------------------------------------------


async def test_learning_context_recovers_stage_next_task_progress_and_blockers() -> None:
    service = await build_service()
    await complete_first(service)
    await service.update_task("task-2", "in_progress")
    await service.update_task("task-2", "blocked", {"reason": "缺 GPU"})

    context = await service.learning_context(PROJECT_ID)

    assert context["project_id"] == PROJECT_ID
    assert context["plan_id"] == PLAN_ID
    assert context["plan_version"] == 1
    assert context["current_stage"] == 1
    assert context["next_task"]["id"] == "task-2"
    assert [item["title"] for item in context["completed"]] == ["任务 task-1"]
    assert context["blockers"][0]["reason"] == "缺 GPU"
    assert context["progress"] == {"done": 1, "total": 3}
    assert context["memory_source"] == "repository"
    assert "任务 task-1" in context["memory_summary"]


async def test_learning_context_prefers_the_learner_scope_when_memory_answers() -> None:
    context = FakeContext(handoff_text="已完成任务「任务 task-1」")
    service = await build_service(context=context)
    await complete_first(service)
    context.prepared.clear()

    recovered = await service.learning_context(PROJECT_ID)

    assert recovered["memory_source"] == "powercontext"
    assert recovered["memory_summary"] == "已完成任务「任务 task-1」"
    assert [call[0] for call in context.prepared] == [f"learner:{PROJECT_ID}"]


async def test_learning_context_falls_back_to_the_repository_when_memory_is_down() -> None:
    service = await build_service(context=DownReadContext())
    await complete_first(service)

    recovered = await service.learning_context(PROJECT_ID)

    assert recovered["memory_source"] == "repository"
    assert recovered["memory_available"] is False
    assert recovered["completed"][0]["evidence"] == EVIDENCE


async def test_learning_context_requires_an_active_plan() -> None:
    service = await build_service(seed_plan=False)

    with pytest.raises(ValueError, match="no active plan"):
        await service.learning_context(PROJECT_ID)


# --- reverse handoff ------------------------------------------------------------


async def test_reverse_handoff_draft_is_built_from_the_durable_events() -> None:
    service = await build_service()
    await complete_first(service)
    await service.update_task("task-2", "in_progress")
    await service.update_task("task-2", "blocked", {"reason": "缺 GPU"})

    draft = await service.reverse_handoff_draft(PROJECT_ID)

    assert draft["objective"] == "Aurora 项目学习接手"
    assert any("已完成任务「任务 task-1」" in item["text"] for item in draft["state"])
    assert any("已复述规则" in item["text"] for item in draft["state"])
    assert draft["state"][0]["citations"][0]["asset_id"] == "ops-1"
    assert draft["next_action"] == "任务 task-2：完成该阶段"
    assert any("缺 GPU" in item for item in draft["omissions"])
    assert draft["disposition"] == "blocked"
    # The draft is exactly what Task 9's Handoff service already accepts.
    assert HandoffDraft.model_validate(draft).objective == draft["objective"]


async def test_reverse_handoff_draft_without_progress_states_an_empty_trail() -> None:
    service = await build_service()

    draft = await service.reverse_handoff_draft(PROJECT_ID)

    assert draft["disposition"] == "continuable"
    assert len(draft["state"]) == 1
    assert "尚无已完成任务" in draft["state"][0]["text"]
    assert draft["next_action"] == "任务 task-1：完成该阶段"


async def test_reverse_handoff_draft_requires_an_active_plan() -> None:
    service = await build_service(seed_plan=False)

    with pytest.raises(ValueError, match="no active plan"):
        await service.reverse_handoff_draft(PROJECT_ID)


# --- HTTP surface ---------------------------------------------------------------


async def test_patch_task_updates_progress_and_reports_rejections() -> None:
    service = await build_service()

    async with client_for(service) as client:
        started = await client.patch("/api/tasks/task-1", json={"status": "in_progress"})
        empty = await client.patch("/api/tasks/task-1", json={"status": "done", "evidence": {}})
        finished = await client.patch(
            "/api/tasks/task-1", json={"status": "done", "evidence": dict(EVIDENCE)}
        )
        recovered = await client.get(f"/api/projects/{PROJECT_ID}/learning-context")
        missing = await client.patch("/api/tasks/task-none", json={"status": "done"})

    assert started.status_code == 200 and started.json()["status"] == "in_progress"
    assert empty.status_code == 409
    error = empty.json()["error"]
    assert error["code"] == "task_update_rejected"
    assert error["details"] == {
        "task_id": "task-1",
        "reason": MISSING_EVIDENCE,
        "missing_count": 1,
    }
    assert finished.status_code == 200 and finished.json()["status"] == "done"
    assert recovered.json()["next_task"]["id"] == "task-2"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "task_not_found"
    assert missing.json()["request_id"] == missing.headers[REQUEST_ID_HEADER]


async def test_patch_task_rejects_an_unknown_status_at_the_boundary() -> None:
    service = await build_service()

    async with client_for(service) as client:
        response = await client.patch("/api/tasks/task-1", json={"status": "archived"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


async def test_replan_endpoint_returns_the_new_version() -> None:
    service = await build_service()

    async with client_for(service) as client:
        await client.patch("/api/tasks/task-1", json={"status": "in_progress"})
        await client.patch("/api/tasks/task-1", json={"status": "done", "evidence": dict(EVIDENCE)})
        response = await client.post(
            f"/api/projects/{PROJECT_ID}/plans/replan", json={"reason": "检查点失败"}
        )

    assert response.status_code == 201, response.text
    plan = response.json()
    assert plan["version"] == 2
    assert plan["tasks"][0]["status"] == "done"
    assert len(plan["tasks"]) == 3


async def test_replan_endpoint_reports_a_rejected_model_answer() -> None:
    service = await build_service(model=ModelReturning(bad_citation_response()))

    async with client_for(service) as client:
        response = await client.post(
            f"/api/projects/{PROJECT_ID}/plans/replan", json={"reason": "资料不足"}
        )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "plan_validation_failed"
    assert error["details"]["reason"] == "unknown_asset"
    assert (await service.repository.get_active_plan(PROJECT_ID)).id == PLAN_ID


async def test_replan_endpoint_refuses_a_project_without_a_plan() -> None:
    service = await build_service(seed_plan=False)

    async with client_for(service) as client:
        response = await client.post(
            f"/api/projects/{PROJECT_ID}/plans/replan", json={"reason": "没有路径"}
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "replan_not_allowed"


async def test_learning_context_endpoint_answers_a_new_session() -> None:
    service = await build_service()
    await complete_first(service)

    async with client_for(service) as client:
        recovered = await client.get(f"/api/projects/{PROJECT_ID}/learning-context")
        missing = await client.get("/api/projects/project-none/learning-context")

    assert recovered.status_code == 200
    body = recovered.json()
    assert body["next_task"]["id"] == "task-2"
    assert body["completed"][0]["title"] == "任务 task-1"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "project_not_found"


# --- reverse handoff over HTTP ---------------------------------------------------


def handoff_content() -> HandoffContent:
    # ``state[].citations`` needs at least one PowerContext source reference, so the fake answers
    # the way the committed Handoff does -- LabRelay's own citations stay in the Source.
    return HandoffContent.model_validate(
        {
            "schema": "powercontext.handoff.v1",
            "objective": "Aurora 项目学习接手",
            "state": [
                {
                    "text": "已完成任务「任务 task-1」",
                    "citations": [
                        {
                            "kind": "source",
                            "source_ref": {"name": "handoff-source", "source_id": "source-1"},
                        }
                    ],
                }
            ],
            "disposition": "continuable",
            "next_action": None,
            "omissions": [],
        }
    )


class RecordingPowerContext:
    """The handful of PowerContext calls ``HandoffService.prepare``/``confirm`` makes."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def remember(self, scope_id, kind, text, reason=None) -> None:
        self.calls.append("remember")

    async def create_source(self, scope_id, request):
        self.calls.append("create_source")
        return SimpleNamespace(source_id="source-1")

    async def create_contract(self, request) -> None:
        self.calls.append("create_contract")

    async def prepare_handoff(self, request):
        self.calls.append("prepare_handoff")
        prepared = PreparedHandoff.model_validate(
            {
                "schema": "powercontext.prepared-handoff.v1",
                "scope_id": f"project:{PROJECT_ID}",
                "base": None,
                "content": handoff_content(),
            }
        )
        return SimpleNamespace(handoff=prepared)

    async def commit_handoff(self, request):
        self.calls.append("commit_handoff")
        return SimpleNamespace(
            reference=ArtifactReference(family="handoff", artifact_id="h1", revision=1),
            content=handoff_content(),
        )


async def test_reverse_handoff_draft_then_confirm_reuses_the_task9_service() -> None:
    service = await build_service()
    await complete_first(service)
    powercontext = RecordingPowerContext()
    handoff = HandoffService(service.repository, powercontext)  # type: ignore[arg-type]

    async with client_for(service, handoff) as client:
        drafted = await client.post(f"/api/projects/{PROJECT_ID}/handoff/draft")
        confirmed = await client.post(
            f"/api/projects/{PROJECT_ID}/handoff/confirm",
            json={"draft_id": drafted.json()["draft_id"]},
        )

    assert drafted.status_code == 200, drafted.text
    body = drafted.json()
    assert body["draft"]["objective"] == "Aurora 项目学习接手"
    assert body["draft"]["state"][0]["text"].startswith("已完成任务「任务 task-1」")
    assert body["provenance"]["events"] == 4
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["handoff_ref"]
    assert powercontext.calls.count("commit_handoff") == 1
    stored = await service.repository.get_project(PROJECT_ID)
    assert stored is not None and stored.handoff_ref == confirmed.json()["handoff_ref"]


async def test_reverse_handoff_draft_needs_a_handoff_service_to_commit() -> None:
    service = await build_service()
    await complete_first(service)

    async with client_for(service) as client:
        response = await client.post(f"/api/projects/{PROJECT_ID}/handoff/draft")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_not_configured"


# --- C1: progress writes stay on the active plan ---------------------------------


async def test_the_same_task_id_is_writable_until_its_plan_is_superseded() -> None:
    """Before/after the replan: the row stops being a progress target, not a row in the store."""
    service = await build_service()
    await complete_first(service)

    started = await service.update_task("task-2", "in_progress")
    assert started.status == "in_progress"

    await service.replan_project(PROJECT_ID, "补排障任务")

    with pytest.raises(TaskTransitionRejected) as failure:
        await service.update_task("task-2", "blocked", {"reason": "缺 GPU"})

    assert failure.value.reason == STALE_PLAN


async def test_a_write_to_a_superseded_task_is_refused_and_writes_nothing() -> None:
    """Fail-closed: no source-task mapping is followed, so a stale id can never extend history."""
    service = await build_service()
    await complete_first(service)
    plan = await service.replan_project(PROJECT_ID, "补排障任务")
    before = await service.repository.list_events(PROJECT_ID)

    with pytest.raises(TaskTransitionRejected) as failure:
        await service.update_task("task-2", "in_progress")

    assert failure.value.reason == STALE_PLAN
    assert (await service.repository.get_task("task-2")).status == "todo"
    assert await service.repository.list_events(PROJECT_ID) == before
    # The active version is exactly what the replan wrote; nothing slipped through the old id.
    assert [task.status for task in (await service.repository.get_plan(plan.id)).tasks] == [
        "done",
        "todo",
        "locked",
    ]


async def test_the_row_the_active_plan_carries_is_the_one_a_learner_writes() -> None:
    """The copy is the live row: its own new evidence and its carried source evidence both read."""
    service = await build_service()
    await complete_first(service)
    plan = await service.replan_project(PROJECT_ID, "补排障任务")
    carried, checkpoint = plan.tasks[0], plan.tasks[1]
    assert (carried.source_task_id, checkpoint.status) == ("task-1", "todo")

    await service.update_task(checkpoint.id, "in_progress")
    finished = await service.update_task(checkpoint.id, "done", {"summary": "跑通排障"})

    assert finished.status == "done"
    recovered = await service.learning_context(PROJECT_ID)
    evidence = {item["task_id"]: item["evidence"] for item in recovered["completed"]}
    assert evidence[carried.id] == EVIDENCE
    assert evidence[checkpoint.id] == {"summary": "跑通排障"}


async def test_patch_task_refuses_a_superseded_task_over_http() -> None:
    service = await build_service()
    await complete_first(service)

    async with client_for(service) as client:
        replanned = await client.post(
            f"/api/projects/{PROJECT_ID}/plans/replan", json={"reason": "补排障任务"}
        )
        refused = await client.patch("/api/tasks/task-2", json={"status": "in_progress"})
        other = await client.patch("/api/tasks/task-3", json={"status": "todo"})

    assert replanned.status_code == 201, replanned.text
    assert refused.status_code == 409
    error = refused.json()["error"]
    assert error["code"] == "task_update_rejected"
    assert error["details"] == {"task_id": "task-2", "reason": STALE_PLAN, "missing_count": 0}
    # A stable answer: the same words for any stale id, and no stored identifier in the message.
    assert other.json()["error"]["message"] == error["message"]
    assert PLAN_ID not in error["message"]
    assert (await service.repository.get_task("task-2")).status == "todo"


# --- I1: history reads follow a carried task back to its source events -----------


async def test_a_recovered_context_reads_carried_work_through_its_source_events() -> None:
    service = await build_service()
    await complete_first(service)
    await service.update_task("task-2", "in_progress")
    await service.update_task("task-2", "skipped", {"reason": "已有基础", "skip_confirmed_by": "x"})

    await service.replan_project(PROJECT_ID, "补排障任务")
    recovered = await service.learning_context(PROJECT_ID)

    assert recovered["plan_version"] == 2
    done_item, skipped_item = recovered["completed"]
    assert done_item["evidence"] == EVIDENCE
    assert skipped_item["status"] == "skipped"
    # I2 as well: a skip reason is read from the skip event, never from a blocked one.
    assert skipped_item["reason"] == "已有基础"
    assert recovered["blockers"] == []
    assert "已复述规则" in recovered["memory_summary"]
    assert "已有基础" in recovered["memory_summary"]


async def test_a_reverse_draft_after_a_replan_still_names_the_evidence_and_the_skip() -> None:
    service = await build_service()
    await complete_first(service)
    await service.update_task("task-2", "in_progress")
    await service.update_task("task-2", "skipped", {"reason": "已有基础", "skip_confirmed_by": "x"})

    await service.replan_project(PROJECT_ID, "补排障任务")
    draft = await service.reverse_handoff_draft(PROJECT_ID)

    assert any(
        "已完成任务" in item["text"] and "已复述规则" in item["text"] for item in draft["state"]
    )
    assert any(
        "已跳过任务" in item["text"] and "已有基础" in item["text"] for item in draft["state"]
    )
    assert not any("未说明" in item["text"] for item in draft["state"])
    assert HandoffDraft.model_validate(draft).state


# --- I2: a skip reason is not a block reason ------------------------------------


async def test_a_skip_reason_is_never_reported_as_a_block_reason() -> None:
    service = await build_service()
    await complete_first(service)
    await service.update_task("task-2", "in_progress")
    await service.update_task("task-2", "skipped", {"reason": "已掌握", "skip_confirmed_by": "x"})
    await service.update_task("task-3", "in_progress")
    await service.update_task("task-3", "blocked", {"reason": "缺 GPU"})

    recovered = await service.learning_context(PROJECT_ID)
    by_status = {item["status"]: item for item in recovered["completed"]}

    assert by_status["skipped"]["reason"] == "已掌握"
    assert by_status["done"]["reason"] == ""
    assert recovered["blockers"][0]["reason"] == "缺 GPU"

    draft = await service.reverse_handoff_draft(PROJECT_ID)
    assert any("已掌握" in item["text"] for item in draft["state"])
    assert draft["omissions"] == ["任务「任务 task-3」受阻：缺 GPU"]


# --- I4: a completion always points at something, and always records it ----------


@pytest.mark.parametrize("evidence", [None, {}, {"summary": "   "}])
async def test_a_completion_needs_one_non_empty_value_even_with_no_declared_requirements(
    evidence: dict[str, Any] | None,
) -> None:
    service = await build_service(
        tasks=[make_task("task-1", 0, (), status="in_progress", evidence_requirements=[])]
    )

    with pytest.raises(TaskTransitionRejected) as failure:
        await service.update_task("task-1", "done", evidence)

    assert failure.value.reason == MISSING_EVIDENCE
    assert await service.repository.list_events(PROJECT_ID) == []

    done = await service.update_task("task-1", "done", {"note": "口述了要点"})

    events = await service.repository.list_events(PROJECT_ID)
    assert done.status == "done"
    assert [event.event_type for event in events] == ["status_changed", "evidence_submitted"]
    assert events[1].payload["evidence"] == {"note": "口述了要点"}


async def test_a_completion_records_its_evidence_once() -> None:
    """The transition is history; the evidence lives in exactly one append-only record."""
    service = await build_service(tasks=[make_task("task-1", 0, (), status="in_progress")])

    await service.update_task("task-1", "done", dict(EVIDENCE))

    status_changed, submitted = await service.repository.list_events(PROJECT_ID)
    assert status_changed.event_type == "status_changed"
    assert status_changed.payload == {"from": "in_progress", "to": "done"}
    assert submitted.event_type == "evidence_submitted"
    assert submitted.payload == {"task_id": "task-1", "evidence": EVIDENCE}


# --- I5: a refusal body says nothing the model wrote -----------------------------


async def test_a_refusal_body_carries_no_stored_text_and_the_log_stays_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Evidence keys are model-authored plan text, so they reach neither body nor log line."""
    service = await build_service(
        tasks=[
            make_task(
                "task-1", 0, (), status="in_progress", evidence_requirements=["模型编造的指标名"]
            )
        ]
    )

    with caplog.at_level(logging.WARNING, logger=ROUTES_LOGGER):
        async with client_for(service) as client:
            response = await client.patch("/api/tasks/task-1", json={"status": "done"})

    error = response.json()["error"]
    assert error["details"] == {
        "task_id": "task-1",
        "reason": MISSING_EVIDENCE,
        "missing_count": 1,
    }
    assert "模型编造的指标名" not in response.text
    assert "任务 task-1" not in response.text
    assert "模型编造的指标名" not in caplog.text
    assert f"reason={MISSING_EVIDENCE}" in caplog.text


# --- M1: the evidence body is bounded and stored once ----------------------------


async def test_an_oversized_evidence_body_never_reaches_the_store() -> None:
    service = await build_service()
    huge = {"summary": "x" * (MAX_EVIDENCE_BYTES + 1)}
    many = {f"key-{index}": "v" for index in range(MAX_EVIDENCE_KEYS + 1)}

    async with client_for(service) as client:
        oversized = await client.patch(
            "/api/tasks/task-1", json={"status": "done", "evidence": huge}
        )
        too_many = await client.patch(
            "/api/tasks/task-1", json={"status": "done", "evidence": many}
        )

    assert oversized.status_code == 422
    assert oversized.json()["error"]["code"] == "validation_failed"
    assert too_many.status_code == 422
    assert await service.repository.list_events(PROJECT_ID) == []


# --- M2: only the transition table answers a conflict ----------------------------


class UnrelatedProgressFailure(LabRelayService):
    """A dependency bug inside a progress write: not bad input, and not a learner conflict."""

    async def update_task(
        self, task_id: str, status: str, evidence: dict[str, Any] | None = None
    ) -> LearningTask:
        raise ValueError("a driver raised this from inside the store")


async def test_an_unrelated_value_error_is_a_server_error_not_a_conflict() -> None:
    service = await build_service()
    broken = UnrelatedProgressFailure(
        service.repository, service.engine, service.context, service.model, service.planner_config
    )

    async with client_for(broken) as client:
        response = await client.patch("/api/tasks/task-1", json={"status": "in_progress"})

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"


async def test_a_transition_the_table_refuses_still_answers_a_stable_conflict() -> None:
    service = await build_service(tasks=[make_task("task-1", 0, (), status="todo")])

    with pytest.raises(IllegalTaskTransition):
        await service.update_task("task-1", "done", dict(EVIDENCE))

    async with client_for(service) as client:
        response = await client.patch(
            "/api/tasks/task-1", json={"status": "done", "evidence": dict(EVIDENCE)}
        )

    error = response.json()["error"]
    assert response.status_code == 409
    assert error["details"] == {
        "task_id": "task-1",
        "reason": "illegal_transition",
        "missing_count": 0,
    }
    assert "Cannot transition" not in error["message"]


# --- M3/M4: the reverse draft survives a finished plan and a long citation list --


async def test_a_finished_plan_is_handed_over_as_complete_not_blocked() -> None:
    service = await build_service(tasks=[make_task("task-1", 0, (), status="todo")])
    await service.update_task("task-1", "in_progress")
    await service.update_task("task-1", "done", dict(EVIDENCE))

    draft = await service.reverse_handoff_draft(PROJECT_ID)

    assert draft["disposition"] == "complete"
    assert HandoffDraft.model_validate(draft).disposition == "complete"
    # A chain nobody can reach is still reported honestly.
    stuck = await build_service(tasks=[make_task("task-2", 1, ("task-1",), status="locked")])
    assert (await stuck.reverse_handoff_draft(PROJECT_ID))["disposition"] == "blocked"


async def test_a_long_citation_list_is_capped_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    service = await build_service(
        tasks=[
            make_task("task-1", 0, (), status="todo"),
            make_task(
                "task-2",
                1,
                ("task-1",),
                citations=[citation_payload("note-1")] * (MAX_HANDOFF_CITATIONS + 6),
            ),
        ]
    )
    await service.update_task("task-1", "in_progress")
    await service.update_task("task-1", "done", dict(EVIDENCE))
    await service.update_task("task-2", "in_progress")
    await service.update_task("task-2", "done", {"summary": "读完两章"})

    with caplog.at_level(logging.WARNING, logger=PROGRESS_LOGGER):
        draft = await service.reverse_handoff_draft(PROJECT_ID)

    assert max(len(item["citations"]) for item in draft["state"]) == MAX_HANDOFF_CITATIONS
    assert "citation overflow" in caplog.text
    assert "dropped=6" in caplog.text
    assert HandoffDraft.model_validate(draft).disposition == "complete"


# --- M5: a replan never waits on the memory service ------------------------------


async def test_a_replan_answers_even_when_the_memory_service_is_down() -> None:
    """Replanning reads the repository and the model; the 503 branch was never reachable."""
    service = await build_service(context=DownReadContext())
    await complete_first(service)

    async with client_for(service) as client:
        response = await client.post(
            f"/api/projects/{PROJECT_ID}/plans/replan", json={"reason": "补排障任务"}
        )

    assert response.status_code == 201, response.text
    assert response.json()["version"] == 2
