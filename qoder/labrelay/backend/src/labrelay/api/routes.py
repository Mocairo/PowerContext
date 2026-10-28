"""The four Task 6 routes: open a project, import its evidence, list it, search it.

Each handler is a call into :class:`labrelay.application.services.LabRelayService` plus the
mapping from a service outcome to an HTTP answer — nothing else. Decisions about what an import
persists, which evidence is searchable and what a memory failure costs belong to Tasks 3-5, and
the responses keep the observability those tasks produced: ``ImportResult`` travels as it is, with
``restricted`` and ``restricted_reasons``, so an operator can see how much evidence was withheld
from the index and why without any file content crossing the wire.

Handoff, assessment, plans, tasks and the question endpoint arrive with Tasks 9-13; this router
declares only what the in-memory service can already answer.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from labrelay.api.deps import ensure_project, get_handoff_service, get_service
from labrelay.api.errors import (
    DEPENDENCY_UNAVAILABLE,
    HANDOFF_NOT_FOUND,
    HANDOFF_NOT_READY,
    IMPORT_PATH_NOT_DIRECTORY,
    ApiError,
    request_id_of,
)
from labrelay.api.schemas import CreateProjectRequest, ImportRequest, SearchRequest
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
