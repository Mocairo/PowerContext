"""The two seams every Task 6 route depends on: the service, and the project it acts on.

``get_service`` reads the ``LabRelayService`` Task 5 built and Task 12 will wire from real
dependencies, so no route imports a repository, an engine or a gateway directly. A missing
service is answered as 503 rather than crashing: production is not runnable until Task 12 supplies
the one dependency factory, and an operator deserves a code for that state instead of a traceback.

``ensure_project`` exists because the routes have no other project lookup to lean on.
``LabRelayService.import_project_files`` raises ``KeyError`` for an unknown project (Task 5), but
``repository.list_assets`` and the engine both answer empty, which would make 「还没有资料」and
「项目不存在」indistinguishable to Task 14. Every project-scoped route calls it first and then works
from the returned project, so one lookup decides both the 404 and the id the route acts on.
"""

from __future__ import annotations

from fastapi import Request

from labrelay.api.errors import PROJECT_NOT_FOUND, SERVICE_NOT_CONFIGURED, ApiError
from labrelay.application.services import LabRelayService
from labrelay.domain.models import Project


def get_service(request: Request) -> LabRelayService:
    service = getattr(request.app.state, "service", None)
    if service is None:
        raise ApiError(
            503,
            SERVICE_NOT_CONFIGURED,
            "No LabRelay service is wired into this application yet.",
            {"dependency": "labrelay_service"},
        )
    return service


async def ensure_project(service: LabRelayService, project_id: str) -> Project:
    """The project behind a path parameter, or a 404 that names it."""
    project = await service.repository.get_project(project_id)
    if project is None:
        raise ApiError(
            404,
            PROJECT_NOT_FOUND,
            f"Project {project_id} does not exist.",
            {"project_id": project_id},
        )
    return project
