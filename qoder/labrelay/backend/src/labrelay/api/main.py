"""The application factory: configuration, API surface and runtime lifecycle.

``create_app`` is the one composition boundary. It builds the real dependency set unless the
caller injects a service for a test/demo, initializes the store on startup, reports dependency
health, and releases the resources it owns on shutdown.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException

from labrelay.api.errors import configure_error_surface
from labrelay.api.routes import build_router
from labrelay.application.handoff import HandoffService
from labrelay.application.services import LabRelayService
from labrelay.config import LabRelayConfig, load_app_config
from labrelay.runtime import Runtime, build_runtime


def _fake_requested(fake_dependencies: bool | None) -> bool:
    if fake_dependencies is not None:
        return fake_dependencies
    return os.getenv("LABRELAY_USE_FAKE_DEPS") == "1"


def create_app(
    config: LabRelayConfig,
    service: LabRelayService | None = None,
    handoff_service: HandoffService | None = None,
    *,
    fake_dependencies: bool | None = None,
) -> FastAPI:
    runtime: Runtime | None = None
    should_build = service is None and (
        fake_dependencies is not None or os.getenv("LABRELAY_USE_FAKE_DEPS") == "1"
    )
    if should_build:
        runtime = build_runtime(config, fake_dependencies=_fake_requested(fake_dependencies))
        service = runtime.service
        handoff_service = handoff_service or runtime.handoff

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.config = config
        app.state.service = service
        app.state.handoff_service = handoff_service
        app.state.runtime = runtime
        if runtime is not None:
            await runtime.repository.initialize()
            initialize_engine = getattr(runtime.engine, "initialize", None)
            if initialize_engine is not None:
                await initialize_engine()
            if not await runtime.repository.health_check():
                raise RuntimeError("seekdb health check failed")
            if not await runtime.context.health_check():
                raise RuntimeError("PowerContext health check failed")
            app.state.ready = True
        yield
        if runtime is not None:
            close_engine = getattr(runtime.engine, "close", None)
            if close_engine is not None:
                await close_engine()
            close_context = getattr(runtime.context, "aclose", None)
            if close_context is not None:
                await close_context()

    app = FastAPI(title="LabRelay", lifespan=lifespan)
    configure_error_surface(app)
    app.state.service = service
    app.state.handoff_service = handoff_service
    app.include_router(build_router())

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        if runtime is None:
            return {"status": "config_ready"}
        if not await runtime.repository.health_check():
            raise HTTPException(status_code=503, detail="seekdb unavailable")
        check_engine = getattr(runtime.engine, "health_check", None)
        if check_engine is not None and not await check_engine():
            raise HTTPException(status_code=503, detail="search engine unavailable")
        if not await runtime.context.health_check():
            raise HTTPException(status_code=503, detail="PowerContext unavailable")
        return {"status": "ready"}

    return app


def create_app_with_service(
    service: LabRelayService, handoff_service: HandoffService | None = None
) -> FastAPI:
    """The full HTTP surface around already-built services for tests and the demo run."""
    return create_app(LabRelayConfig(), service, handoff_service)


def create_default_app() -> FastAPI:
    """Build the app from the repository's YAML and environment for uvicorn."""
    project_root = Path(__file__).resolve().parents[4]
    _load_dotenv(project_root / ".env")
    return create_app(load_app_config(project_root), fake_dependencies=False)


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(path)
