"""The routes the offline API answers on: projects, evidence, retrieval, Handoff, plans, progress.

Each handler is a call into :class:`labrelay.application.services.LabRelayService` plus the
mapping from a service outcome to an HTTP answer — nothing else. Decisions about what an import
persists, which evidence is searchable, what a memory failure costs and which progress write is
legal belong to Tasks 3-5 and 9-11, and the responses keep the observability those tasks
produced: ``ImportResult`` travels as it is, with ``restricted`` and ``restricted_reasons``, so an
operator can see how much evidence was withheld from the index and why without any file content
crossing the wire.

Task 11 added the learner-facing half: ``PATCH /api/tasks/{id}``, the replan call, the
``learning-context`` recovery read and the reverse Handoff draft. The question endpoint and the
export page arrive with Tasks 12 and 13.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from labrelay.api.deps import ensure_assessment, ensure_project, get_handoff_service, get_service
from labrelay.api.errors import (
    DEPENDENCY_UNAVAILABLE,
    HANDOFF_NOT_FOUND,
    HANDOFF_NOT_READY,
    IMPORT_PATH_NOT_DIRECTORY,
    PLAN_NOT_FOUND,
    PLAN_VALIDATION_FAILED,
    REPLAN_NOT_ALLOWED,
    TASK_NOT_FOUND,
    TASK_UPDATE_REJECTED,
    ApiError,
    request_id_of,
)
from labrelay.api.schemas import (
    AssessmentRequest,
    CreateProjectRequest,
    GeneratePlanRequest,
    ImportRequest,
    ReplanRequest,
    SearchRequest,
    TaskUpdateRequest,
)
from labrelay.application.handoff import (
    HandoffAcknowledgementRequest,
    HandoffConfirmRequest,
    HandoffDraft,
    HandoffNotReadyError,
    HandoffService,
    TaskOutcomeRequest,
)
from labrelay.application.services import ContextServiceUnavailableError, LabRelayService
from labrelay.context.powercontext import PowerContextUnavailableError
from labrelay.learning.progress import (
    ILLEGAL_TRANSITION,
    NoActivePlanError,
    TaskTransitionRejected,
)
from labrelay.planning.validator import PlanValidationError

logger = logging.getLogger("labrelay.api.routes")

# ``Annotated`` rather than ``= Depends(...)``: the same dependency, without the call in a default
# argument that bugbear (and FastAPI's own guidance) discourage.
ServiceDep = Annotated[LabRelayService, Depends(get_service)]
HandoffDep = Annotated[HandoffService, Depends(get_handoff_service)]

IMPORT_PATH_MESSAGE = "The import path must be an existing directory."


def _import_refused(path: Path) -> ApiError:
    """The one 422 an unusable import source can produce, whatever noticed it."""
    return ApiError(422, IMPORT_PATH_NOT_DIRECTORY, IMPORT_PATH_MESSAGE, {"path": str(path)})


def _powercontext_unavailable(project_id: str | None = None) -> ApiError:
    details: dict[str, Any] = {"dependency": "powercontext"}
    if project_id:
        details["project_id"] = project_id
    return ApiError(
        503,
        DEPENDENCY_UNAVAILABLE,
        "The PowerContext service is unavailable.",
        details,
    )


def build_router() -> APIRouter:
    """A router that takes its collaborators from ``app.state`` rather than from a closure."""
    router = APIRouter(prefix="/api")

    @router.post("/projects", status_code=201)
    async def create_project(
        payload: CreateProjectRequest, service: ServiceDep, request: Request
    ) -> dict[str, Any]:
        try:
            project = await service.create_project(
                payload.title, payload.research_area, payload.description
            )
        except ContextServiceUnavailableError as exc:
            # The gateway handed back empty or missing scope ids, so the project has no memory
            # identity and was never persisted. That is the one dependency outage this route is
            # told about (spec 17.3 reserves 503 for it) — an arbitrary ``ValueError`` is a bug
            # or bad input and stays a 500. The exception's message is not logged and not
            # echoed: the identifiers in this line are what an operator can act on.
            logger.warning(
                "dependency failure operation=create_project request_id=%s project_id=%s "
                "dependency=context_gateway dependency_status=unavailable exception_class=%s",
                request_id_of(request),
                exc.project_id,
                type(exc).__name__,
            )
            raise ApiError(
                503,
                DEPENDENCY_UNAVAILABLE,
                "The memory service did not hand back usable project scopes.",
                {"dependency": "context_gateway"},
            ) from None
        except PowerContextUnavailableError:
            logger.warning(
                "dependency failure operation=create_project request_id=%s "
                "dependency=powercontext dependency_status=unavailable",
                request_id_of(request),
            )
            raise _powercontext_unavailable() from None
        return project.model_dump()

    @router.post("/projects/{project_id}/imports")
    async def import_files(
        project_id: str, payload: ImportRequest, service: ServiceDep, request: Request
    ) -> dict[str, Any]:
        # The project is checked first, so an unknown project always answers 404 whatever the
        # body says, and the route reads its id from the loaded project rather than the URL.
        project = await ensure_project(service, project_id)
        path = Path(payload.path).expanduser().resolve()
        if not path.is_dir():
            raise _import_refused(path)
        try:
            result = await service.import_project_files(project.id, path)
        except (OSError, ValueError) as exc:
            # ``import_directory`` re-reads the path, so a folder that went away or lost its
            # permissions since the precheck arrives here. It must not become a 500, and only
            # these two families are mapped: a ``RuntimeError`` or ``TypeError`` from a real bug
            # still reaches the middleware as one.
            logger.warning(
                "import source unavailable operation=import_files request_id=%s project_id=%s "
                "exception_class=%s",
                request_id_of(request),
                project.id,
                type(exc).__name__,
            )
            raise _import_refused(path) from None
        return dict(result)

    @router.get("/projects/{project_id}/assets")
    async def list_assets(project_id: str, service: ServiceDep) -> list[dict[str, Any]]:
        project = await ensure_project(service, project_id)
        assets = await service.repository.list_assets(project.id)
        return [asset.model_dump() for asset in assets]

    @router.post("/projects/{project_id}/search")
    async def search(
        project_id: str, payload: SearchRequest, service: ServiceDep
    ) -> dict[str, Any]:
        project = await ensure_project(service, project_id)
        hits = await service.search(
            project.id, payload.query, list(payload.asset_kinds), payload.limit
        )
        return {"hits": [hit.model_dump() for hit in hits]}

    @router.post("/projects/{project_id}/assessments", status_code=201)
    async def submit_assessment(
        project_id: str, payload: AssessmentRequest, service: ServiceDep
    ) -> dict[str, Any]:
        # The project is resolved from the URL and the assessment id is minted by the service, so
        # neither half of the identity comes from the body (Task 10).
        project = await ensure_project(service, project_id)
        assessment = await service.submit_assessment(project.id, payload.model_dump())
        return assessment.model_dump(mode="json")

    @router.post("/projects/{project_id}/plans", status_code=201)
    async def generate_plan(
        project_id: str,
        payload: GeneratePlanRequest,
        service: ServiceDep,
        request: Request,
    ) -> dict[str, Any]:
        project = await ensure_project(service, project_id)
        await ensure_assessment(service, project, payload.assessment_id)
        try:
            plan = await service.generate_plan(project.id, payload.assessment_id)
        except PlanValidationError as exc:
            # The model answer is not a plan: it broke one of the rules Task 10 owns. The reason
            # code is what the client branches on; the validator's own text (which can name a
            # task or asset id from model output) stays in the log line, never in the body.
            logger.warning(
                "plan rejected operation=generate_plan request_id=%s project_id=%s reason=%s",
                request_id_of(request),
                project.id,
                exc.reason,
            )
            raise ApiError(
                422,
                PLAN_VALIDATION_FAILED,
                "The generated plan did not pass validation.",
                {"reason": exc.reason, "project_id": project.id},
            ) from None
        except PowerContextUnavailableError:
            # Reading the committed Handoff is a PowerContext call, so its outage keeps the
            # status Task 9 already uses for it.
            raise _powercontext_unavailable(project.id) from None
        return plan.model_dump(mode="json")

    @router.get("/plans/{plan_id}")
    async def get_plan(plan_id: str, service: ServiceDep) -> dict[str, Any]:
        """One plan version by id, including the drafts a material gap left behind."""
        plan = await service.repository.get_plan(plan_id)
        if plan is None:
            raise ApiError(
                404,
                PLAN_NOT_FOUND,
                f"Plan {plan_id} does not exist.",
                {"plan_id": plan_id},
            )
        return plan.model_dump(mode="json")

    @router.patch("/tasks/{task_id}")
    async def update_task(
        task_id: str,
        payload: TaskUpdateRequest,
        service: ServiceDep,
        request: Request,
    ) -> dict[str, Any]:
        """Record one piece of progress: a start, a completion with evidence, a block, a skip.

        The status travels as the domain literal, so an invented one is FastAPI's 422 and never
        reaches the store. Everything else is a refusal about *state*, and answers 409 with the rule
        in ``details.reason`` — the learner sees which half was missing without this route having to
        repeat the task's evidence requirements back at it.
        """
        try:
            task = await service.update_task(task_id, payload.status, payload.evidence)
        except KeyError:
            # Same answer as an unknown plan: the id may exist in another project's namespace, and
            # confirming that would publish somebody else's identifier space (spec 19.1).
            raise ApiError(
                404, TASK_NOT_FOUND, f"Task {task_id} does not exist.", {"task_id": task_id}
            ) from None
        except TaskTransitionRejected as exc:
            logger.warning(
                "progress update refused operation=update_task request_id=%s task_id=%s reason=%s",
                request_id_of(request),
                task_id,
                exc.reason,
            )
            raise ApiError(
                409,
                TASK_UPDATE_REJECTED,
                str(exc),
                {"task_id": task_id, "reason": exc.reason},
            ) from None
        except ValueError as exc:
            # Task 5's transition table: the pair of statuses is simply not a legal move.
            logger.warning(
                "progress update refused operation=update_task request_id=%s task_id=%s "
                "reason=%s exception_class=%s",
                request_id_of(request),
                task_id,
                ILLEGAL_TRANSITION,
                type(exc).__name__,
            )
            raise ApiError(
                409,
                TASK_UPDATE_REJECTED,
                "This task cannot move to that status from where it is.",
                {"task_id": task_id, "reason": ILLEGAL_TRANSITION},
            ) from None
        return task.model_dump(mode="json")

    @router.post("/projects/{project_id}/plans/replan", status_code=201)
    async def replan_plan(
        project_id: str,
        payload: ReplanRequest,
        service: ServiceDep,
        request: Request,
    ) -> dict[str, Any]:
        """Replace the unfinished part of the path with the next immutable plan version."""
        project = await ensure_project(service, project_id)
        try:
            plan = await service.replan_project(project.id, payload.reason)
        except NoActivePlanError:
            raise ApiError(
                409,
                REPLAN_NOT_ALLOWED,
                f"Project {project.id} has no active plan to replan.",
                {"project_id": project.id},
            ) from None
        except PlanValidationError as exc:
            # The model's remainder is not a plan. Nothing was written: the active version, its
            # tasks and its whole event trail are still what the learner was working on.
            logger.warning(
                "replan rejected operation=replan_plan request_id=%s project_id=%s reason=%s",
                request_id_of(request),
                project.id,
                exc.reason,
            )
            raise ApiError(
                422,
                PLAN_VALIDATION_FAILED,
                "The replanned tasks did not pass validation.",
                {"reason": exc.reason, "project_id": project.id},
            ) from None
        except PowerContextUnavailableError:
            raise _powercontext_unavailable(project.id) from None
        return plan.model_dump(mode="json")

    @router.get("/projects/{project_id}/learning-context")
    async def learning_context(
        project_id: str, service: ServiceDep, request: Request
    ) -> dict[str, Any]:
        """Where a new session picks up: stage, next task, finished work, blockers, and the note.

        Always a 200 when the project has a plan, memory reachable or not — the facts come from the
        repository, and ``memory_source`` / ``memory_available`` say how much of the prose behind
        them was actually recovered (FR-PRG-003, FR-PRG-005).
        """
        project = await ensure_project(service, project_id)
        try:
            return await service.learning_context(project.id)
        except NoActivePlanError:
            logger.info(
                "no active plan operation=learning_context request_id=%s project_id=%s",
                request_id_of(request),
                project.id,
            )
            raise ApiError(
                404,
                PLAN_NOT_FOUND,
                f"Project {project.id} has no active plan yet.",
                {"project_id": project.id},
            ) from None

    @router.post("/projects/{project_id}/handoff/draft")
    async def reverse_handoff_draft(
        project_id: str,
        service: ServiceDep,
        handoff: HandoffDep,
        request: Request,
    ) -> dict[str, Any]:
        """Draft the reverse Handoff from the progress record, and stage it for confirmation.

        This is the one place the four fields are *assembled*; committing them stays Task 9's job.
        The draft is handed to :meth:`HandoffService.prepare`, which is what makes the existing
        ``/handoff/confirm`` the only way a PowerContext revision is ever made — a learner confirms
        what the events say, and no second commit path appears here.
        """
        project = await ensure_project(service, project_id)
        try:
            draft = await service.reverse_handoff_draft(project.id)
        except NoActivePlanError:
            raise ApiError(
                404,
                PLAN_NOT_FOUND,
                f"Project {project.id} has no active plan yet.",
                {"project_id": project.id},
            ) from None
        provenance = dict(draft.get("provenance") or {})
        body = {key: value for key, value in draft.items() if key != "provenance"}
        try:
            validated = HandoffDraft.model_validate(body)
        except ValueError as exc:
            # Stored progress that cannot be phrased as a draft is a readiness problem, not a bug:
            # only the exception class is logged, since the message quotes the stored text.
            logger.warning(
                "reverse draft refused operation=reverse_handoff_draft request_id=%s project_id=%s "
                "exception_class=%s",
                request_id_of(request),
                project.id,
                type(exc).__name__,
            )
            raise ApiError(
                409,
                HANDOFF_NOT_READY,
                "The recorded progress cannot be phrased as a Handoff draft yet.",
                {"project_id": project.id},
            ) from None
        preview = await handoff.prepare(project.id, validated)
        return {
            "draft_id": preview.draft_id,
            "draft": validated.model_dump(mode="json"),
            "provenance": provenance,
        }

    @router.post("/projects/{project_id}/handoff/prepare")
    async def prepare_handoff(
        project_id: str, payload: HandoffDraft, handoff: HandoffDep, service: ServiceDep
    ) -> dict[str, Any]:
        project = await ensure_project(service, project_id)
        preview = await handoff.prepare(project.id, payload)
        return preview.model_dump(mode="json")

    @router.post("/projects/{project_id}/handoff/confirm")
    async def confirm_handoff(
        project_id: str,
        handoff: HandoffDep,
        service: ServiceDep,
        payload: HandoffConfirmRequest | None = None,
    ) -> dict[str, Any]:
        project = await ensure_project(service, project_id)
        try:
            view = await handoff.confirm(
                project.id,
                draft_id=payload.draft_id if payload else None,
                draft=payload.draft if payload else None,
            )
        except HandoffNotReadyError as exc:
            raise ApiError(
                409,
                HANDOFF_NOT_READY,
                str(exc),
                {"project_id": project.id},
            ) from None
        except PowerContextUnavailableError:
            raise _powercontext_unavailable(project.id) from None
        return view.model_dump(mode="json")

    @router.get("/projects/{project_id}/handoff")
    async def get_handoff(
        project_id: str, handoff: HandoffDep, service: ServiceDep
    ) -> dict[str, Any]:
        project = await ensure_project(service, project_id)
        try:
            view = await handoff.get(project.id)
        except PowerContextUnavailableError:
            raise _powercontext_unavailable(project.id) from None
        if view is None:
            raise ApiError(
                404,
                HANDOFF_NOT_FOUND,
                f"No committed Handoff exists for project {project.id}.",
                {"project_id": project.id},
            )
        return view.model_dump(mode="json")

    @router.post("/projects/{project_id}/handoff/acknowledge")
    async def acknowledge_handoff(
        project_id: str,
        payload: HandoffAcknowledgementRequest,
        handoff: HandoffDep,
        service: ServiceDep,
    ):
        project = await ensure_project(service, project_id)
        try:
            result = await handoff.acknowledge(project.id, payload)
        except HandoffNotReadyError as exc:
            raise ApiError(
                409,
                HANDOFF_NOT_READY,
                str(exc),
                {"project_id": project.id},
            ) from None
        except PowerContextUnavailableError:
            raise _powercontext_unavailable(project.id) from None
        return result

    @router.post("/projects/{project_id}/handoff/outcome")
    async def record_outcome(
        project_id: str,
        payload: TaskOutcomeRequest,
        handoff: HandoffDep,
        service: ServiceDep,
    ):
        project = await ensure_project(service, project_id)
        try:
            result = await handoff.record_outcome(project.id, payload)
        except HandoffNotReadyError as exc:
            raise ApiError(
                409,
                HANDOFF_NOT_READY,
                str(exc),
                {"project_id": project.id},
            ) from None
        except PowerContextUnavailableError:
            raise _powercontext_unavailable(project.id) from None
        return result

    return router
