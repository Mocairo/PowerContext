"""Browser uploads: staging, import, rejection reasons and in-place re-upload."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from labrelay.api.main import create_app_with_service
from labrelay.application.services import LabRelayService
from labrelay.importers import uploads as uploads_module
from labrelay.persistence.memory import InMemoryRepository
from labrelay.retrieval.in_memory import InMemorySearchEngine


class FakeModel:
    dimension = 2

    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


class FakeContext:
    async def ensure_scopes(self, project):
        return f"project:{project.id}", f"learner:{project.id}"

    async def remember(self, scope_id, kind, text, reason=None):
        return None

    async def prepare_context(self, scope_id, query, max_bytes):
        return None, None


def make_service() -> LabRelayService:
    model = FakeModel()
    return LabRelayService(
        repository=InMemoryRepository(),
        engine=InMemorySearchEngine(model),
        context=FakeContext(),
        model=model,
    )


@asynccontextmanager
async def client_for(service: LabRelayService) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app_with_service(service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture(autouse=True)
def upload_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LABRELAY_DATA_ROOT", str(tmp_path / "data"))
    return tmp_path / "data"


async def create_project(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/api/projects",
        json={"title": "Aurora", "research_area": "DL", "description": "handoff"},
    )
    assert response.status_code == 201
    return response.json()["id"]


async def test_upload_imports_and_searches_files(upload_root: Path) -> None:
    service = make_service()
    async with client_for(service) as client:
        project_id = await create_project(client)
        response = await client.post(
            f"/api/projects/{project_id}/uploads",
            files=[
                ("files", ("notes.md", "# 数据缓存\n旧缓存会污染数据划分。\n", "text/markdown")),
                ("files", ("metrics.csv", "run_key,val_accuracy\nbaseline,0.70\n", "text/csv")),
            ],
        )

    assert response.status_code == 201
    body = response.json()
    assert body["assets"] == 2
    assert body["chunks"] >= 2
    assert body["restricted"] == 0
    assert body["rejected"] == []
    assert {item["name"] for item in body["files"]} == {"notes.md", "metrics.csv"}
    staged = upload_root / "uploads" / project_id
    assert (staged / "notes.md").is_file()
    assert (staged / "metrics.csv").is_file()

    async with client_for(service) as client:
        hits = await client.post(
            f"/api/projects/{project_id}/search",
            json={"query": "数据缓存", "limit": 5},
        )
    assert hits.status_code == 200
    assert hits.json()["hits"][0]["citation"]["title"] == "notes.md"


async def test_upload_rejects_bad_names_and_keeps_the_good_files(
    upload_root: Path,
) -> None:
    service = make_service()
    async with client_for(service) as client:
        project_id = await create_project(client)
        response = await client.post(
            f"/api/projects/{project_id}/uploads",
            files=[
                ("files", ("../../escape.md", "# escaped\n", "text/markdown")),
                ("files", ("payload.exe", b"MZ\x00", "application/octet-stream")),
                ("files", ("keep.txt", "a small note\n", "text/plain")),
            ],
        )

    assert response.status_code == 201
    body = response.json()
    assert body["assets"] == 2
    assert {item["name"] for item in body["files"]} == {"escape.md", "keep.txt"}
    assert body["rejected"] == [{"name": "payload.exe", "reason": "unsupported_extension"}]
    project_dir = (upload_root / "uploads" / project_id).resolve()
    assert (project_dir / "escape.md").is_file()
    assert not (upload_root / "escape.md").exists()
    assert not (upload_root / "uploads" / "escape.md").exists()


async def test_upload_restricts_credential_files(upload_root: Path) -> None:
    service = make_service()
    async with client_for(service) as client:
        project_id = await create_project(client)
        response = await client.post(
            f"/api/projects/{project_id}/uploads",
            files=[
                ("files", ("notes.md", "# clean note\n", "text/markdown")),
                ("files", ("config.md", "password = supersecret123\n", "text/markdown")),
            ],
        )
        assets = await client.get(f"/api/projects/{project_id}/assets")

    assert response.status_code == 201
    body = response.json()
    assert body["assets"] == 2
    assert body["restricted"] == 1
    restricted = next(iter(body["restricted_reasons"]))
    assert body["restricted_reasons"][restricted] == ["password_assignment"]
    assert {item["name"]: item["status"] for item in body["files"]} == {
        "notes.md": "active",
        "config.md": "restricted",
    }
    statuses = {asset["title"]: asset["status"] for asset in assets.json()}
    assert statuses == {"notes.md": "active", "config.md": "restricted"}


async def test_upload_with_only_rejected_files_is_422(upload_root: Path) -> None:
    service = make_service()
    async with client_for(service) as client:
        project_id = await create_project(client)
        response = await client.post(
            f"/api/projects/{project_id}/uploads",
            files=[("files", ("payload.exe", b"MZ\x00", "application/octet-stream"))],
        )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "upload_rejected"
    assert error["details"]["rejected"] == [
        {"name": "payload.exe", "reason": "unsupported_extension"}
    ]


async def test_upload_reports_oversized_file(
    upload_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(uploads_module, "MAX_UPLOAD_BYTES", 16)
    service = make_service()
    async with client_for(service) as client:
        project_id = await create_project(client)
        response = await client.post(
            f"/api/projects/{project_id}/uploads",
            files=[
                ("files", ("note.md", b"x" * 64, "text/markdown")),
                ("files", ("ok.md", b"ok\n", "text/markdown")),
            ],
        )

    assert response.status_code == 201
    body = response.json()
    assert body["assets"] == 1
    assert body["rejected"] == [{"name": "note.md", "reason": "too_large"}]


async def test_reupload_replaces_the_staged_file_and_asset(upload_root: Path) -> None:
    service = make_service()
    async with client_for(service) as client:
        project_id = await create_project(client)
        first = await client.post(
            f"/api/projects/{project_id}/uploads",
            files=[("files", ("notes.md", "# 第一版\n旧内容\n", "text/markdown"))],
        )
        second = await client.post(
            f"/api/projects/{project_id}/uploads",
            files=[("files", ("notes.md", "# 第二版\n新内容\n", "text/markdown"))],
        )
        hits = await client.post(
            f"/api/projects/{project_id}/search",
            json={"query": "新内容", "limit": 5},
        )

    assert first.status_code == second.status_code == 201
    assert second.json()["assets"] == 1
    staged = upload_root / "uploads" / project_id / "notes.md"
    assert "第二版" in staged.read_text(encoding="utf-8")
    assert hits.json()["hits"][0]["citation"]["excerpt"].startswith("# 第二版")


async def test_upload_limits_the_request_to_twenty_files(upload_root: Path) -> None:
    service = make_service()
    async with client_for(service) as client:
        project_id = await create_project(client)
        response = await client.post(
            f"/api/projects/{project_id}/uploads",
            files=[
                ("files", (f"note-{index}.md", b"note\n", "text/markdown")) for index in range(21)
            ],
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "upload_rejected"


async def test_upload_into_unknown_project_is_404(upload_root: Path) -> None:
    service = make_service()
    async with client_for(service) as client:
        response = await client.post(
            "/api/projects/missing/uploads",
            files=[("files", ("note.md", b"note\n", "text/markdown"))],
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "project_not_found"
    assert not upload_root.exists()
