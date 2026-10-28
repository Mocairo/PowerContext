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

from fastapi import APIRouter, Depends

from labrelay.api.deps import get_service, require_project
from labrelay.api.errors import (
    DEPENDENCY_UNAVAILABLE,
    IMPORT_PATH_NOT_DIRECTORY,
    PROJECT_NOT_FOUND,
    ApiError,
)
from labrelay.api.schemas import CreateProjectRequest, ImportRequest, SearchRequest
from labrelay.application.services import LabRelayService

logger = logging.getLogger("labrelay.api.routes")

# ``Annotated`` rather than ``= Depends(...)``: the same dependency, without the call in a default
# argument that bugbear (and FastAPI's own guidance) discourage.
ServiceDep = Annotated[LabRelayService, Depends(get_service)]


def build_router() -> APIRouter:
    """A router that takes its collaborators from ``app.state`` rather than from a closure."""
    router = APIRouter(prefix="/api")

    @router.post("/projects", status_code=201)
    async def create_project(payload: CreateProjectRequest, service: ServiceDep) -> dict[str, Any]:
        try:
            project = await service.create_project(
                payload.title, payload.research_area, payload.description
            )
        except ValueError as exc:
            # Task 5 raises exactly one ``ValueError`` here: the gateway handed back empty or
            # missing scope ids, so the project has no memory identity and was never persisted.
            # That is a dependency answer (spec 17.3 reserves 503 for it), not bad input, and the
            # message stays server-side because it names the refused project id.
            logger.warning(
                "dependency failure operation=create_project dependency_status=unavailable "
                "exception_class=%s",
                type(exc).__name__,
            )
            raise ApiError(
                503,
                DEPENDENCY_UNAVAILABLE,
                "The memory service did not hand back usable project scopes.",
                {"dependency": "context_gateway"},
            ) from None
        return project.model_dump()

    @router.post("/projects/{project_id}/imports")
    async def import_files(
        project_id: str, payload: ImportRequest, service: ServiceDep
    ) -> dict[str, Any]:
        path = Path(payload.path).expanduser().resolve()
        if not path.is_dir():
            raise ApiError(
                422,
                IMPORT_PATH_NOT_DIRECTORY,
                "The import path must be an existing directory.",
                {"path": str(path)},
            )
        try:
            result = await service.import_project_files(project_id, path)
        except KeyError:
            raise ApiError(
                404,
                PROJECT_NOT_FOUND,
                f"Project {project_id} does not exist.",
                {"project_id": project_id},
            ) from None
        return dict(result)

    @router.get("/projects/{project_id}/assets")
    async def list_assets(project_id: str, service: ServiceDep) -> list[dict[str, Any]]:
        await require_project(service, project_id)
        assets = await service.repository.list_assets(project_id)
        return [asset.model_dump() for asset in assets]

    @router.post("/projects/{project_id}/search")
    async def search(
        project_id: str, payload: SearchRequest, service: ServiceDep
    ) -> dict[str, Any]:
        await require_project(service, project_id)
        hits = await service.search(
            project_id, payload.query, list(payload.asset_kinds), payload.limit
        )
        return {"hits": [hit.model_dump() for hit in hits]}

    return router
