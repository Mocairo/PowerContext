import asyncio
from pathlib import Path

import httpx

from labrelay.api.main import create_app
from labrelay.config import load_app_config
from labrelay.persistence.memory import InMemoryRepository
from labrelay.retrieval.in_memory import InMemorySearchEngine
from labrelay.runtime import build_service

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_build_service_uses_fake_dependencies() -> None:
    service = build_service(load_app_config(PROJECT_ROOT), fake_dependencies=True)
    assert isinstance(service.repository, InMemoryRepository)
    assert isinstance(service.engine, InMemorySearchEngine)


def test_health_ready_checks_runtime_dependencies() -> None:
    app = create_app(load_app_config(PROJECT_ROOT), fake_dependencies=True)

    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                return await client.get("/health/ready")

    response = asyncio.run(run())
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
