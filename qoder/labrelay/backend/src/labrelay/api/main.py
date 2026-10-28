"""The application factory: configuration, the Task 6 API surface and the error envelope.

``create_app`` grows only in one direction here — it now accepts the ``LabRelayService`` Task 5
built and mounts the router on top of it. It still does not construct a repository, an engine or a
gateway: that factory is Task 12's job, and until then a production app answers every API request
with ``service_not_configured`` (503) instead of guessing at dependencies.

The service is attached when the app is built rather than inside the lifespan, so the routes are
usable as soon as the object exists; ``app.state.config`` keeps the startup assignment Task 1 chose.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from labrelay.api.errors import configure_error_surface
from labrelay.api.routes import build_router
from labrelay.application.services import LabRelayService
from labrelay.config import LabRelayConfig


def create_app(config: LabRelayConfig, service: LabRelayService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.config = config
        yield

    app = FastAPI(title="LabRelay", lifespan=lifespan)
    configure_error_surface(app)
    app.state.service = service
    app.include_router(build_router())

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        return {"status": "config_ready"}

    return app


def create_app_with_service(service: LabRelayService) -> FastAPI:
    """The full HTTP surface around an already-built service.

    Tests and the demo run use this instead of a dependency factory: the service they hand in
    carries its own in-memory repository, engine and gateway double, and the defaults in
    :class:`LabRelayConfig` are enough because no route reads the config yet.
    """
    return create_app(LabRelayConfig(), service)
