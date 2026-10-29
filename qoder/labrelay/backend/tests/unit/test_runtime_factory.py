import asyncio
from pathlib import Path

import httpx

from labrelay.api.main import create_app, create_default_app
from labrelay.config import load_app_config
from labrelay.context.memory import InMemoryContextGateway
from labrelay.context.powercontext import PowerContextService
from labrelay.model.fake import DeterministicModelGateway
from labrelay.persistence.memory import InMemoryRepository
from labrelay.retrieval.in_memory import InMemorySearchEngine
from labrelay.runtime import build_service

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_build_service_uses_fake_dependencies() -> None:
    service = build_service(load_app_config(PROJECT_ROOT), fake_dependencies=True)
    assert isinstance(service.repository, InMemoryRepository)
    assert isinstance(service.engine, InMemorySearchEngine)


def test_create_default_app_selects_the_offline_dependencies_from_the_environment(
    monkeypatch,
) -> None:
    """``LABRELAY_USE_FAKE_DEPS=1`` is what makes the documented demo run need no keys."""
    monkeypatch.setenv("LABRELAY_USE_FAKE_DEPS", "1")
    service = create_default_app().state.service

    assert isinstance(service.repository, InMemoryRepository)
    assert isinstance(service.engine, InMemorySearchEngine)
    assert isinstance(service.context, InMemoryContextGateway)
    assert isinstance(service.model, DeterministicModelGateway)


def test_create_default_app_without_the_flag_keeps_the_real_context(monkeypatch) -> None:
    """The flag is the whole switch: with the endpoints configured, nothing fake is built."""
    monkeypatch.delenv("LABRELAY_USE_FAKE_DEPS", raising=False)
    monkeypatch.setenv("GENERATION_BASE_URL", "http://model.invalid/v1")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://model.invalid/v1")
    service = create_default_app().state.service

    assert isinstance(service.context, PowerContextService)
    assert not isinstance(service.repository, InMemoryRepository)


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
