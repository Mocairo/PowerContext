"""Task 6: the REST surface for project creation, import, assets and search.

Every test drives the real ``LabRelayService`` on top of the real ``InMemoryRepository``, the
real ``InMemorySearchEngine`` and the real importer, so the routes are exercised against the
behaviour Tasks 3-5 shipped rather than against mock responses. ``ContextGateway`` is still
faked (PowerContext is Task 9) and one repository subclass raises on a single method, which is
how a dead seekdb connection will look to this layer (Task 7).

Two rulings shape the surface beyond the plan's snippets: every request gets a generated
identifier echoed as ``X-LabRelay-Request-Id`` — a caller-supplied one is replaced, never adopted —
and every error — route-raised or framework-raised — is returned as
``{"error": {"code", "message", "details"}, "request_id"}`` with no exception text in it
(spec 17.2, 19.1). A dependency outage is recognised only from the application layer's own typed
error, never from an arbitrary ``ValueError``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from labrelay.api.errors import REQUEST_ID_HEADER
from labrelay.api.main import create_app, create_app_with_service
from labrelay.api.schemas import SearchRequest
from labrelay.application.services import ContextServiceUnavailableError, LabRelayService
from labrelay.config import LabRelayConfig
from labrelay.importers.assets import ImportResult
from labrelay.persistence.memory import InMemoryRepository
from labrelay.retrieval.in_memory import InMemorySearchEngine

APP_CONFIG = Path(__file__).resolve().parents[3] / "configs" / "app.yaml"
RETRIEVAL_CONFIG = Path(__file__).resolve().parents[3] / "configs" / "retrieval.yaml"
API_LOGGER = "labrelay.api.errors"
ROUTES_LOGGER = "labrelay.api.routes"
# What a caller might try to smuggle into the response header and the log line.
INBOUND_REQUEST_ID = "request-forged-by-the-caller"


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

    async def project_context_summary(self, project_scope_id, learner_scope_id, query):
        return None, "local_progress"

    async def health_check(self):
        return True


class EmptyScopeContext(FakeContext):
    """The gateway answers with no usable scope ids, so the service refuses the project."""

    async def ensure_scopes(self, project):
        return "", ""


class ValueErrorContext(FakeContext):
    """A gateway that raises a plain ``ValueError``, which is not a declared outage."""

    async def ensure_scopes(self, project):
        raise ValueError("PowerContext token is not a valid UUID")


class ExplodingRepository(InMemoryRepository):
    """A store that fails the way an unreachable seekdb will: loudly, with a secret in it."""

    message = "seekdb connection failed password=super-secret-value"

    async def save_project(self, project) -> None:
        raise RuntimeError(self.message)


class VanishingDirectoryService(LabRelayService):
    """The route's precheck passed and then the folder went away: the importer's own guard fires.

    ``import_directory`` re-reads ``is_dir()`` and raises this very ``ValueError``, but making the
    filesystem change mid-request is not reproducible, so the window is replayed at the seam.
    """

    failure: Exception = ValueError("Import source /tmp/gone is not an existing directory")

    async def import_project_files(self, project_id: str, source_path: Path) -> ImportResult:
        raise self.failure


class BrokenImportService(LabRelayService):
    """An import that fails for a reason that is not the path, which must stay a 500."""

    async def import_project_files(self, project_id: str, source_path: Path) -> ImportResult:
        raise RuntimeError("seekdb connection failed password=super-secret-value")


def make_service(
    *,
    repository: InMemoryRepository | None = None,
    context: FakeContext | None = None,
    service_class: type[LabRelayService] = LabRelayService,
) -> LabRelayService:
    model = FakeModel()
    return service_class(
        repository=repository if repository is not None else InMemoryRepository(),
        engine=InMemorySearchEngine(model),
        context=context if context is not None else FakeContext(),
        model=model,
    )


@asynccontextmanager
async def client_for(service: LabRelayService) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app_with_service(service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def create_project(client: httpx.AsyncClient, title: str = "Aurora") -> str:
    created = await client.post(
        "/api/projects",
        json={"title": title, "research_area": "DL", "description": "baseline"},
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def write_note(root: Path, name: str, text: str) -> Path:
    """Write ``name``, which may carry sub-directories, under ``root``."""
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def assert_envelope(body: dict[str, Any], request_id: str, code: str) -> dict[str, Any]:
    """The one shape spec 17.2 allows, so no handler can drift into ``{"detail": ...}``."""
    assert set(body) == {"error", "request_id"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    assert isinstance(body["error"]["details"], dict)
    assert body["request_id"] == request_id
    return body["error"]


# --- the plan's Task 6 test ----------------------------------------------------


async def test_create_import_and_search_round_trip(tmp_path: Path) -> None:
    write_note(
        tmp_path / "notes",
        "baseline.md",
        "# baseline\nRun train.py with configs/baseline.yaml and compare metrics.",
    )

    async with client_for(make_service()) as client:
        created = await client.post(
            "/api/projects",
            json={"title": "Aurora", "research_area": "DL", "description": "baseline"},
        )
        project_id = created.json()["id"]
        imported = await client.post(
            f"/api/projects/{project_id}/imports",
            json={"path": str(tmp_path)},
        )
        searched = await client.post(
            f"/api/projects/{project_id}/search",
            json={"query": "baseline metrics", "asset_kinds": [], "limit": 5},
        )

    assert created.status_code == 201
    assert imported.json()["assets"] == 1
    assert searched.status_code == 200
    assert searched.json()["hits"][0]["citation"]["kind"] == "note"


# --- request identifier --------------------------------------------------------


async def test_every_response_carries_a_generated_request_id(tmp_path: Path) -> None:
    write_note(tmp_path, "baseline.md", "run the baseline and compare metrics\n")

    async with client_for(make_service()) as client:
        project_id = await create_project(client)
        responses = [
            await client.post(f"/api/projects/{project_id}/imports", json={"path": str(tmp_path)}),
            await client.get(f"/api/projects/{project_id}/assets"),
            await client.post(f"/api/projects/{project_id}/search", json={"query": "baseline"}),
            await client.post("/api/projects", json={}),
            await client.get("/api/projects/nope/assets"),
        ]

    ids = [response.headers[REQUEST_ID_HEADER] for response in responses]
    assert all(request_id.startswith("request-") for request_id in ids)
    assert len(set(ids)) == len(ids), "one identifier per request, never reused"
    # The identifier is echoed only as a header on successes; errors repeat it in the body.
    assert responses[-1].json()["request_id"] == ids[-1]


async def test_error_envelope_repeats_the_identifier_of_its_own_response(
    tmp_path: Path,
) -> None:
    async with client_for(make_service()) as client:
        response = await client.post("/api/projects/missing/imports", json={"path": str(tmp_path)})

    request_id = response.headers[REQUEST_ID_HEADER]
    error = assert_envelope(response.json(), request_id, "project_not_found")
    assert error["details"] == {"project_id": "missing"}


async def test_an_inbound_request_id_is_replaced_never_adopted() -> None:
    """The identifier is a server promise: a caller cannot put its own text in header or body."""
    async with client_for(make_service()) as client:
        forged = {REQUEST_ID_HEADER: INBOUND_REQUEST_ID}
        not_found = await client.get("/api/projects/project-none/assets", headers=forged)
        validated = await client.post("/api/projects", json={}, headers=forged)
        created = await client.post(
            "/api/projects",
            json={"title": "Aurora", "research_area": "DL", "description": "x"},
            headers=forged,
        )

    ids = [response.headers[REQUEST_ID_HEADER] for response in (not_found, validated, created)]
    assert all(request_id != INBOUND_REQUEST_ID for request_id in ids)
    assert all(request_id.startswith("request-") for request_id in ids)
    assert len(set(ids)) == len(ids), "two callers using one id still get two ids"
    assert INBOUND_REQUEST_ID not in not_found.text + validated.text + created.text
    # The body of an error names the very id its own header carries, never the inbound one.
    assert_envelope(not_found.json(), ids[0], "project_not_found")
    assert_envelope(validated.json(), ids[1], "validation_failed")


async def test_a_missing_project_outranks_an_unusable_import_path(tmp_path: Path) -> None:
    """The project behind the URL is checked first, so the answer never depends on the path."""
    a_file = write_note(tmp_path, "baseline.md", "run the baseline\n")

    async with client_for(make_service()) as client:
        response = await client.post(
            "/api/projects/project-none/imports", json={"path": str(a_file)}
        )

    assert response.status_code == 404
    assert_envelope(response.json(), response.headers[REQUEST_ID_HEADER], "project_not_found")


# --- 404 / 422 mapping ---------------------------------------------------------


async def test_import_of_a_path_that_is_not_a_directory_is_rejected(tmp_path: Path) -> None:
    note = write_note(tmp_path, "baseline.md", "run the baseline\n")

    async with client_for(make_service()) as client:
        project_id = await create_project(client)
        a_file = await client.post(f"/api/projects/{project_id}/imports", json={"path": str(note)})
        missing = await client.post(
            f"/api/projects/{project_id}/imports", json={"path": str(tmp_path / "nope")}
        )

    assert a_file.status_code == 422
    assert missing.status_code == 422
    for response in (a_file, missing):
        error = assert_envelope(
            response.json(), response.headers[REQUEST_ID_HEADER], "import_path_not_directory"
        )
        assert set(error["details"]) == {"path"}


@pytest.mark.parametrize(
    ("payload", "location"),
    [
        ({}, "body.title"),
        ({"title": "Aurora", "research_area": "DL"}, "body.description"),
        ({"title": "Aurora", "research_area": "DL", "description": 7}, "body.description"),
    ],
)
async def test_a_project_body_that_does_not_validate_uses_the_envelope(
    payload: dict[str, Any], location: str
) -> None:
    async with client_for(make_service()) as client:
        response = await client.post("/api/projects", json=payload)

    assert response.status_code == 422
    error = assert_envelope(
        response.json(), response.headers[REQUEST_ID_HEADER], "validation_failed"
    )
    locations = {item["loc"]: item["type"] for item in error["details"]["errors"]}
    assert location in locations
    # Only the field path and the pydantic error type travel: never the rejected input.
    assert all(set(item) == {"loc", "type"} for item in error["details"]["errors"])


async def test_a_malformed_json_body_is_a_validation_error() -> None:
    async with client_for(make_service()) as client:
        response = await client.post(
            "/api/projects/project-1/search",
            content=b"{not json",
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 422
    error = assert_envelope(
        response.json(), response.headers[REQUEST_ID_HEADER], "validation_failed"
    )
    assert [item["type"] for item in error["details"]["errors"]] == ["json_invalid"]


async def test_an_empty_import_path_never_reaches_the_filesystem(tmp_path: Path) -> None:
    """``""`` would resolve to the server's own working directory, so the schema refuses it."""
    async with client_for(make_service()) as client:
        project_id = await create_project(client)
        response = await client.post(f"/api/projects/{project_id}/imports", json={"path": ""})

    assert response.status_code == 422
    error = assert_envelope(
        response.json(), response.headers[REQUEST_ID_HEADER], "validation_failed"
    )
    assert [item["loc"] for item in error["details"]["errors"]] == ["body.path"]


async def test_an_asset_kind_the_importer_cannot_produce_is_refused(tmp_path: Path) -> None:
    """The domain literal guards the edge, so a typo is a 422 and not a quietly empty search."""
    write_note(tmp_path, "baseline.md", "run the baseline and compare metrics\n")
    async with client_for(make_service()) as client:
        project_id = await create_project(client)
        await client.post(f"/api/projects/{project_id}/imports", json={"path": str(tmp_path)})
        response = await client.post(
            f"/api/projects/{project_id}/search",
            json={"query": "baseline", "asset_kinds": ["spreadsheet"]},
        )

    assert response.status_code == 422
    error = assert_envelope(
        response.json(), response.headers[REQUEST_ID_HEADER], "validation_failed"
    )
    assert [item["loc"] for item in error["details"]["errors"]] == ["body.asset_kinds.0"]


@pytest.mark.parametrize(
    ("method", "path", "json"),
    [
        ("GET", "/api/projects/{id}/assets", None),
        ("POST", "/api/projects/{id}/search", {"query": "baseline"}),
        ("POST", "/api/projects/{id}/imports", {"path": "."}),
    ],
)
async def test_every_project_scoped_route_reports_a_missing_project_as_404(
    method: str, path: str, json: dict[str, Any] | None
) -> None:
    async with client_for(make_service()) as client:
        response = await client.request(method, path.format(id="project-none"), json=json)

    assert response.status_code == 404
    assert_envelope(response.json(), response.headers[REQUEST_ID_HEADER], "project_not_found")


async def test_framework_errors_also_use_the_envelope() -> None:
    async with client_for(make_service()) as client:
        unknown = await client.get("/api/nothing-here")
        wrong_method = await client.get("/api/projects")

    assert unknown.status_code == 404
    assert wrong_method.status_code == 405
    assert_envelope(unknown.json(), unknown.headers[REQUEST_ID_HEADER], "not_found")
    assert_envelope(
        wrong_method.json(), wrong_method.headers[REQUEST_ID_HEADER], "method_not_allowed"
    )


# --- responses the UI and Task 14 read -----------------------------------------


async def test_assets_endpoint_lists_the_project_assets(tmp_path: Path) -> None:
    write_note(tmp_path, "baseline.md", "# baseline\nrun train.py and compare metrics\n")
    write_note(tmp_path, "notes/operations.md", "# operations\nthe gpu server queue\n")

    async with client_for(make_service()) as client:
        project_id = await create_project(client)
        imported = await client.post(
            f"/api/projects/{project_id}/imports", json={"path": str(tmp_path)}
        )
        assets = await client.get(f"/api/projects/{project_id}/assets")

    assert imported.json()["assets"] == 2
    assert assets.status_code == 200
    assert isinstance(assets.json(), list)
    assert {item["title"] for item in assets.json()} == {"baseline.md", "operations.md"}
    assert {item["kind"] for item in assets.json()} == {"note", "operations"}
    assert {item["project_id"] for item in assets.json()} == {project_id}
    assert {item["status"] for item in assets.json()} == {"active"}
    assert all("uri" in item and "checksum" in item for item in assets.json())


async def test_import_response_keeps_the_restricted_observability(tmp_path: Path) -> None:
    write_note(tmp_path, "baseline.md", "# baseline\nrun train.py and compare metrics\n")
    secret = write_note(tmp_path, "credentials.yaml", "api_key: super-secret-value\n")

    async with client_for(make_service()) as client:
        project_id = await create_project(client)
        imported = await client.post(
            f"/api/projects/{project_id}/imports", json={"path": str(tmp_path)}
        )
        searched = await client.post(
            f"/api/projects/{project_id}/search", json={"query": "credentials"}
        )

    body = imported.json()
    assert set(body) == {
        "assets",
        "chunks",
        "failed",
        "skipped",
        "restricted",
        "restricted_reasons",
    }
    assert (body["assets"], body["restricted"]) == (2, 1)
    assert list(body["restricted_reasons"].values()) == [["api_secret"]]
    assert len(body["restricted_reasons"]) == body["restricted"]
    # The pattern name is the whole story: no file content and no blocked path leaves here.
    assert "super-secret-value" not in imported.text
    assert str(secret) not in imported.text
    # The restricted asset is stored but never retrievable, so only clean evidence answers.
    assert {hit["citation"]["title"] for hit in searched.json()["hits"]} <= {"baseline.md"}


async def test_search_honours_the_asset_kind_filter_and_limit(tmp_path: Path) -> None:
    write_note(tmp_path, "train.py", "def run_baseline():\n    return 1\n")
    write_note(tmp_path, "notes/baseline.md", "baseline notes for the sweep\n")

    async with client_for(make_service()) as client:
        project_id = await create_project(client)
        await client.post(f"/api/projects/{project_id}/imports", json={"path": str(tmp_path)})
        all_hits = await client.post(
            f"/api/projects/{project_id}/search",
            json={"query": "baseline", "asset_kinds": [], "limit": 12},
        )
        code_only = await client.post(
            f"/api/projects/{project_id}/search",
            json={"query": "baseline", "asset_kinds": ["code"], "limit": 12},
        )
        one = await client.post(
            f"/api/projects/{project_id}/search", json={"query": "baseline", "limit": 1}
        )

    assert all_hits.status_code == 200
    assert {hit["citation"]["kind"] for hit in all_hits.json()["hits"]} == {"code", "note"}
    assert {hit["citation"]["kind"] for hit in code_only.json()["hits"]} == {"code"}
    assert len(one.json()["hits"]) == 1
    assert all(hit["score"] >= 0 for hit in all_hits.json()["hits"])


# --- dependency failures -------------------------------------------------------


async def test_an_unexpected_dependency_failure_returns_a_redacted_internal_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A dead store must not answer with its own traceback, message or credentials."""
    with caplog.at_level(logging.ERROR, logger=API_LOGGER):
        async with client_for(make_service(repository=ExplodingRepository())) as client:
            response = await client.post(
                "/api/projects",
                json={"title": "Aurora", "research_area": "DL", "description": "x"},
            )

    assert response.status_code == 500
    error = assert_envelope(response.json(), response.headers[REQUEST_ID_HEADER], "internal_error")
    assert error["details"] == {}
    assert "super-secret-value" not in response.text
    assert "seekdb connection failed" not in response.text
    assert "RuntimeError" in caplog.text
    assert "super-secret-value" not in caplog.text


async def test_a_gateway_without_scopes_is_reported_as_an_unavailable_dependency(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=ROUTES_LOGGER):
        async with client_for(make_service(context=EmptyScopeContext())) as client:
            response = await client.post(
                "/api/projects",
                json={"title": "Aurora", "research_area": "DL", "description": "x"},
            )

    assert response.status_code == 503
    request_id = response.headers[REQUEST_ID_HEADER]
    error = assert_envelope(response.json(), request_id, "dependency_unavailable")
    assert error["details"] == {"dependency": "context_gateway"}
    # The service's own text (which names the refused project id) is not echoed back.
    assert "non-empty memory scope ids" not in response.text
    # The warning line is the operator's whole story: which request, which refused project.
    assert f"request_id={request_id}" in caplog.text
    assert "project_id=project-" in caplog.text
    assert "non-empty memory scope ids" not in caplog.text


async def test_the_service_refuses_unusable_scopes_with_its_typed_dependency_error() -> None:
    """The narrow exception is what the API maps to 503, and it carries the refused id."""
    with pytest.raises(ContextServiceUnavailableError) as refused:
        await make_service(context=EmptyScopeContext()).create_project("Aurora", "DL", "one")

    assert refused.value.project_id.startswith("project-")
    # Task 5's contract still holds: this is a ValueError, so nothing catching those broke.
    assert isinstance(refused.value, ValueError)


@pytest.mark.parametrize(
    "failure",
    [
        ValueError("Import source /tmp/gone is not an existing directory"),
        OSError(20, "Not a directory"),
    ],
    ids=["value_error", "os_error"],
)
async def test_an_import_path_that_fails_after_the_precheck_is_still_a_422(
    tmp_path: Path, failure: Exception
) -> None:
    """The route and the importer read `is_dir()` at different moments; both answer 422."""
    write_note(tmp_path, "baseline.md", "run the baseline and compare metrics\n")
    service = make_service(service_class=VanishingDirectoryService)
    service.failure = failure

    async with client_for(service) as client:
        project_id = await create_project(client)
        response = await client.post(
            f"/api/projects/{project_id}/imports", json={"path": str(tmp_path)}
        )

    assert response.status_code == 422
    error = assert_envelope(
        response.json(), response.headers[REQUEST_ID_HEADER], "import_path_not_directory"
    )
    assert set(error["details"]) == {"path"}
    assert Path(error["details"]["path"]).name == tmp_path.name
    assert str(failure) not in response.text


async def test_an_import_that_fails_for_another_reason_is_still_an_internal_error(
    tmp_path: Path,
) -> None:
    """Only path failures are a 422: a programming or store error must not be re-labelled."""
    write_note(tmp_path, "baseline.md", "run the baseline and compare metrics\n")

    async with client_for(make_service(service_class=BrokenImportService)) as client:
        project_id = await create_project(client)
        response = await client.post(
            f"/api/projects/{project_id}/imports", json={"path": str(tmp_path)}
        )

    assert response.status_code == 500
    error = assert_envelope(response.json(), response.headers[REQUEST_ID_HEADER], "internal_error")
    assert error["details"] == {}
    assert "super-secret-value" not in response.text


async def test_an_ordinary_value_error_from_the_gateway_is_not_called_an_outage() -> None:
    """503 is a claim about a dependency: only the application layer's typed error may make it."""
    async with client_for(make_service(context=ValueErrorContext())) as client:
        response = await client.post(
            "/api/projects",
            json={"title": "Aurora", "research_area": "DL", "description": "x"},
        )

    assert response.status_code == 500
    assert_envelope(response.json(), response.headers[REQUEST_ID_HEADER], "internal_error")
    assert "valid UUID" not in response.text


async def test_an_api_without_a_wired_service_refuses_requests() -> None:
    app = create_app(LabRelayConfig())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/projects",
            json={"title": "Aurora", "research_area": "DL", "description": "x"},
        )

    assert response.status_code == 503
    assert_envelope(response.json(), response.headers[REQUEST_ID_HEADER], "service_not_configured")


# --- configuration consistency and the untouched health route ------------------


def test_request_id_header_and_search_limit_match_the_shipped_config() -> None:
    """Same convention as Tasks 3 and 4: constants are pinned to the YAML they mirror."""
    app_config = yaml.safe_load(APP_CONFIG.read_text(encoding="utf-8"))
    retrieval_config = yaml.safe_load(RETRIEVAL_CONFIG.read_text(encoding="utf-8"))

    assert app_config["server"]["request_id_header"] == REQUEST_ID_HEADER
    assert (
        SearchRequest.model_fields["limit"].default
        == retrieval_config["retrieval"]["fusion"]["output_limit"]
    )


async def test_health_ready_still_answers_and_now_carries_the_request_id() -> None:
    app = create_app(LabRelayConfig())
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "config_ready"}
    assert response.headers[REQUEST_ID_HEADER].startswith("request-")


def test_the_openapi_document_lists_exactly_the_task_6_surface() -> None:
    """Task 14 generates its client from this document, so the annotations must survive it."""
    paths = create_app_with_service(make_service()).openapi()["paths"]

    assert {key for key in paths if key.startswith("/api")} == {
        "/api/projects",
        "/api/projects/{project_id}/imports",
        "/api/projects/{project_id}/assets",
        "/api/projects/{project_id}/search",
    }
