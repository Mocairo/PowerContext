from types import SimpleNamespace

import httpx
from powercontext.http import (
    ArtifactReference,
    HandoffContent,
    PreparedHandoff,
    SourceReference,
)

from labrelay.api.main import create_app_with_service
from labrelay.application.handoff import HandoffService
from labrelay.application.services import LabRelayService
from labrelay.context.memory import InMemoryContextGateway
from labrelay.context.powercontext import PowerContextUnavailableError
from labrelay.domain.models import Project
from labrelay.persistence.memory import InMemoryRepository
from labrelay.retrieval.in_memory import InMemorySearchEngine


class FakeModel:
    dimension = 2

    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]

    async def generate_json(self, system_prompt, user_payload):
        return {}


def make_content() -> HandoffContent:
    return HandoffContent.model_validate(
        {
            "schema": "powercontext.handoff.v1",
            "objective": "复现 baseline",
            "state": [
                {
                    "text": "已完成 baseline",
                    "citations": [
                        {
                            "kind": "source",
                            "source_ref": {"name": "handoff-source", "source_id": "source-1"},
                        }
                    ],
                }
            ],
            "disposition": "continuable",
            "next_action": None,
            "omissions": [],
        }
    )


class ApiContext:
    def __init__(self) -> None:
        self.content = make_content()
        self.prepared = PreparedHandoff.model_validate(
            {
                "schema": "powercontext.prepared-handoff.v1",
                "scope_id": "project-scope-1",
                "base": None,
                "content": self.content,
            }
        )
        self.reference = ArtifactReference(family="handoff", artifact_id="h1", revision=1)

    async def remember(self, scope_id, kind, text, reason=None):
        return None

    async def create_source(self, scope_id, request):
        return SimpleNamespace(source_id="source-1")

    async def create_contract(self, request):
        return SimpleNamespace(kind="work-contract")

    async def prepare_handoff(self, request):
        return SimpleNamespace(handoff=self.prepared)

    async def commit_handoff(self, request):
        return SimpleNamespace(reference=self.reference, content=self.content)

    async def continue_handoff(self, request):
        return SimpleNamespace(content=self.content)

    async def acknowledge_handoff(self, request):
        return SimpleNamespace(
            receipt=SimpleNamespace(
                source=SourceReference(name="handoff-receipt", source_id="ack-1")
            )
        )

    async def record_outcome(self, request):
        return {"source": "recorded"}


def build_app(context=None):
    repository = InMemoryRepository()
    project = Project(
        id="project-1",
        title="Aurora",
        research_area="DL",
        description="baseline",
        project_scope_id="project-scope-1",
        learner_scope_id="learner-scope-1",
    )
    repository.projects[project.id] = project
    service = LabRelayService(
        repository=repository,
        engine=InMemorySearchEngine(FakeModel()),
        context=InMemoryContextGateway(),
        model=FakeModel(),
    )
    handoff_service = HandoffService(repository, context or ApiContext())
    return create_app_with_service(service, handoff_service), repository


DRAFT = {
    "objective": "复现 baseline",
    "state": [
        {
            "text": "已完成 baseline",
            "citations": [
                {
                    "asset_id": "asset-1",
                    "kind": "code",
                    "title": "train.py",
                    "uri": "repo/train.py",
                    "version": "sha",
                    "locator": {"line_start": 1},
                }
            ],
        }
    ],
    "next_action": "推进扩展实验",
    "omissions": ["旧数据缓存可能影响数据划分"],
}


async def test_handoff_prepare_confirm_get_ack_and_outcome() -> None:
    app, repository = build_app()
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            prepared = await client.post("/api/projects/project-1/handoff/prepare", json=DRAFT)
            assert prepared.status_code == 200
            prepared_body = prepared.json()
            assert prepared_body["persisted"] is False
            assert prepared_body["draft_id"]

            committed = await client.post(
                "/api/projects/project-1/handoff/confirm",
                json={"draft_id": prepared_body["draft_id"]},
            )
            assert committed.status_code == 200
            body = committed.json()
            assert body["handoff_ref"]
            assert body["content"]["objective"] == "复现 baseline"
            stored = await repository.get_project("project-1")
            assert stored is not None
            assert stored.handoff_ref == body["handoff_ref"]

            fetched = await client.get("/api/projects/project-1/handoff")
            assert fetched.status_code == 200
            assert fetched.json()["content"]["objective"] == "复现 baseline"

            acknowledged = await client.post(
                "/api/projects/project-1/handoff/acknowledge",
                json={
                    "receiver": "new-student",
                    "receiver_checks": {
                        "live_state": "confirmed",
                        "capability": "confirmed",
                        "authorization": "confirmed",
                    },
                },
            )
            assert acknowledged.status_code == 200
            assert acknowledged.json()["receipt_ref"]

            outcome = await client.post(
                "/api/projects/project-1/handoff/outcome",
                json={
                    "objective": "复现 baseline",
                    "status": "succeeded",
                    "summary": "指标一致",
                    "observations": ["baseline 指标已核对"],
                    "remaining_work": ["扩展实验"],
                },
            )
            assert outcome.status_code == 200
            assert outcome.json()["source"] == "recorded"


class FailingCommitContext(ApiContext):
    async def commit_handoff(self, request):
        raise PowerContextUnavailableError("powercontext is down")


async def test_powercontext_outage_is_503_and_preserves_handoff_ref() -> None:
    app, repository = build_app(FailingCommitContext())
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            prepared = await client.post("/api/projects/project-1/handoff/prepare", json=DRAFT)
            draft_id = prepared.json()["draft_id"]
            failed = await client.post(
                "/api/projects/project-1/handoff/confirm", json={"draft_id": draft_id}
            )

    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "dependency_unavailable"
    stored = await repository.get_project("project-1")
    assert stored is not None
    assert stored.handoff_ref is None


async def test_invalid_receiver_checks_are_422() -> None:
    app, _ = build_app()
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            prepared = await client.post("/api/projects/project-1/handoff/prepare", json=DRAFT)
            draft_id = prepared.json()["draft_id"]
            await client.post(
                "/api/projects/project-1/handoff/confirm", json={"draft_id": draft_id}
            )
            response = await client.post(
                "/api/projects/project-1/handoff/acknowledge",
                json={
                    "receiver": "new-student",
                    "receiver_checks": {"live_state": "bogus"},
                },
            )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


async def test_accepted_acknowledgement_without_confirmed_checks_is_422() -> None:
    app, _ = build_app()
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            prepared = await client.post("/api/projects/project-1/handoff/prepare", json=DRAFT)
            draft_id = prepared.json()["draft_id"]
            await client.post(
                "/api/projects/project-1/handoff/confirm", json={"draft_id": draft_id}
            )
            missing = await client.post(
                "/api/projects/project-1/handoff/acknowledge",
                json={"receiver": "new-student"},
            )
            unconfirmed = await client.post(
                "/api/projects/project-1/handoff/acknowledge",
                json={
                    "receiver": "new-student",
                    "receiver_checks": {"live_state": "confirmed"},
                },
            )
            silent_decline = await client.post(
                "/api/projects/project-1/handoff/acknowledge",
                json={"receiver": "new-student", "status": "declined"},
            )

    assert missing.status_code == 422
    assert unconfirmed.status_code == 422
    assert silent_decline.status_code == 422


async def test_handoff_errors_are_structured() -> None:
    app, _ = build_app()
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            missing = await client.get("/api/projects/project-1/handoff")
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "handoff_not_found"

            not_ready = await client.post("/api/projects/project-1/handoff/confirm")
            assert not_ready.status_code == 409
            assert not_ready.json()["error"]["code"] == "handoff_not_ready"

            unknown = await client.get("/api/projects/missing/handoff")
            assert unknown.status_code == 404
            assert unknown.json()["error"]["code"] == "project_not_found"
