from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from labrelay.config import LabRelayConfig


def create_app(config: LabRelayConfig) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.config = config
        yield

    app = FastAPI(title="LabRelay", lifespan=lifespan)

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        return {"status": "config_ready"}

    return app
