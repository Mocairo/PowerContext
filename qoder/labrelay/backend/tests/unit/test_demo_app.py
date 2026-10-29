"""The local demo panel's endpoints, mounted on the one application factory.

These routes exist so the static showcase can call a real backend from ``127.0.0.1``. They are
ported from the showcase branch, but on top of the trunk's single model gateway: the panel asks
the *configured* gateway a question and never a second, Qoder-specific implementation.
"""

from pathlib import Path

import httpx

from labrelay.api.main import create_app_with_service
from labrelay.application.handoff import HandoffService
from labrelay.application.services import LabRelayService
from labrelay.context.memory import InMemoryContextGateway
from labrelay.model.fake import DeterministicModelGateway
from labrelay.persistence.memory import InMemoryRepository
from labrelay.retrieval.in_memory import InMemorySearchEngine


class FailingModel(DeterministicModelGateway):
    async def generate_json(self, system_prompt, user_payload):
        del system_prompt, user_payload
        raise ValueError("the generation channel is unreachable")


def _service(model=None) -> LabRelayService:
    repository = InMemoryRepository()
    gateway = model if model is not None else DeterministicModelGateway()
    return LabRelayService(
        repository=repository,
        engine=InMemorySearchEngine(gateway),
        context=InMemoryContextGateway(),
        model=gateway,
    )


def _app(model=None):
    service = _service(model)
    return create_app_with_service(service, HandoffService(service.repository, service.context))


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_demo_flow_bootstrap_search_handoff_model() -> None:
    app = _app()
    async with app.router.lifespan_context(app):
        async with _client(app) as client:
            status = await client.get("/api/demo/status")
            assert status.status_code == 200
            body = status.json()
            assert body["backend"] == "memory"
            assert body["context"] == "memory"
            assert body["model_provider"] == "deterministic"
            assert Path(body["demo_project_root"]).is_dir()

            bootstrap = await client.post("/api/demo/bootstrap")
            assert bootstrap.status_code == 200
            created = bootstrap.json()
            project_id = created["project_id"]
            # The corpus ships the credential canary, so restricted is expected to be non-zero:
            # the panel has to show that withheld evidence is the normal case, not a failure.
            assert created["assets"] >= 13
            assert created["chunks"] > 0
            assert created["restricted"] == 1
            assert created["draft_id"]
            assert [citation["title"] for citation in created["citations"]] == [
                "metrics.csv",
                "debugging.md",
            ]

            search = await client.post(
                f"/api/projects/{project_id}/search",
                json={"query": "数据缓存 问题 baseline", "limit": 5},
            )
            hits = search.json()["hits"]
            assert {"debugging.md", "metrics.csv"} & {hit["citation"]["title"] for hit in hits}

            confirmed = await client.post(
                f"/api/projects/{project_id}/handoff/confirm",
                json={"draft_id": created["draft_id"]},
            )
            assert confirmed.status_code == 200
            assert confirmed.json()["handoff_ref"]

            fetched = await client.get(f"/api/projects/{project_id}/handoff")
            assert fetched.json()["content"]["objective"] == "复现 baseline，并推进学习率消融实验"

            model = await client.post("/api/demo/model", json={"prompt": "下一步做什么"})
            assert model.status_code == 200
            assert model.json()["model"] == "DeterministicModelGateway"
            assert set(model.json()["result"]) == {"headline", "next_step"}


async def test_demo_model_failure_is_503_not_a_crash() -> None:
    """A probe endpoint must report an unreachable channel as unavailable, never as a 500."""
    app = _app(FailingModel())
    async with app.router.lifespan_context(app):
        async with _client(app) as client:
            response = await client.post("/api/demo/model")

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "dependency_unavailable"
    assert body["error"]["details"]["dependency"] == "model"


def test_demo_app_allows_only_local_site_origins() -> None:
    middleware = [
        layer for layer in _app().user_middleware if layer.cls.__name__ == "CORSMiddleware"
    ]
    assert middleware
    origins = middleware[0].kwargs["allow_origins"]
    assert "http://127.0.0.1:4173" in origins
    assert "http://localhost:5173" in origins
    assert "*" not in origins
