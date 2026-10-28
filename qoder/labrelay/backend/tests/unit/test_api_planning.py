"""Task 10's HTTP surface: submit an assessment, generate a plan, read a plan back.

The routes stay as thin as Task 6's: one service call plus the mapping from a service outcome to
an answer. The three contracts this file pins down are the ones Task 14 builds its screens on —

* an assessment is filed under a server-minted id and never under one the caller chose,
* a plan is only ever returned in the state the validator approved (``active`` with executable
  roots, or ``draft`` with ``needs_material`` gaps),
* every rejection answers in the Task 6 envelope with a stable code, and never echoes model
  output or an exception message (spec 17.2, 19.1).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from labrelay.api.errors import REQUEST_ID_HEADER
from labrelay.api.main import create_app_with_service
from labrelay.application.services import LabRelayService
from labrelay.context.powercontext import PowerContextUnavailableError
from labrelay.model.fake import DeterministicModelGateway
from tests.helpers_planning import (
    PROJECT_ID,
    FakeContext,
    make_assets,
    make_service,
    task_payload,
    valid_assessment_payload,
)

ERRORS_LOGGER = "labrelay.api.errors"
ASSESSMENT_BODY = valid_assessment_payload()


@asynccontextmanager
async def client_for(service: LabRelayService) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app_with_service(service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def make_project(client: httpx.AsyncClient) -> str:
    created = await client.post(
        "/api/projects",
        json={"title": "Aurora", "research_area": "DL", "description": "baseline"},
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def submit_assessment(client: httpx.AsyncClient, project_id: str) -> dict[str, Any]:
    response = await client.post(f"/api/projects/{project_id}/assessments", json=ASSESSMENT_BODY)
    assert response.status_code == 201, response.text
    return response.json()


def assert_envelope(body: dict[str, Any], request_id: str, code: str) -> dict[str, Any]:
    assert set(body) == {"error", "request_id"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == code
    assert body["request_id"] == request_id
    return body["error"]


async def deterministic_service(**kwargs: Any) -> LabRelayService:
    """A service whose model derives the plan from the evidence the planner sends."""
    service, _ = await make_service(
        assets=make_assets(), model=DeterministicModelGateway(), **kwargs
    )
    return service


# --- POST /api/projects/{id}/assessments ---------------------------------------


async def test_an_assessment_is_filed_under_a_server_minted_id() -> None:
    service, _ = await make_service(assets=make_assets(), model_response={})

    async with client_for(service) as client:
        project_id = await make_project(client)
        created = await client.post(f"/api/projects/{project_id}/assessments", json=ASSESSMENT_BODY)

    assert created.status_code == 201
    body = created.json()
    assert body["id"].startswith("assessment-")
    assert body["project_id"] == project_id
    assert {key: body[key] for key in ASSESSMENT_BODY} == ASSESSMENT_BODY
    # The scoring the planner reads is part of what the client gets back.
    assert body["needs_foundations"] is False
    assert body["paper_depth"] == 90
    assert await service.repository.get_assessment(body["id"]) is not None


async def test_an_assessment_body_cannot_choose_its_own_identity() -> None:
    service, _ = await make_service(assets=make_assets(), model_response={})

    async with client_for(service) as client:
        project_id = await make_project(client)
        created = await client.post(
            f"/api/projects/{project_id}/assessments",
            json={**ASSESSMENT_BODY, "id": "assessment-forged", "project_id": "project-evil"},
        )

    assert created.json()["id"] != "assessment-forged"
    assert created.json()["project_id"] == project_id
    assert await service.repository.get_assessment("assessment-forged") is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("python_level", "guru"),
        ("paper_level", "skimmed"),
        ("target", "publish"),
        ("hours_per_week", 0),
        ("hours_per_week", 200),
        ("hours_per_week", "ten"),
    ],
)
async def test_an_assessment_answer_the_domain_refuses_is_a_422(field: str, value: Any) -> None:
    service, _ = await make_service(assets=make_assets(), model_response={})

    async with client_for(service) as client:
        project_id = await make_project(client)
        response = await client.post(
            f"/api/projects/{project_id}/assessments",
            json={**ASSESSMENT_BODY, field: value},
        )

    assert response.status_code == 422
    error = assert_envelope(
        response.json(), response.headers[REQUEST_ID_HEADER], "validation_failed"
    )
    assert [item["loc"] for item in error["details"]["errors"]] == [f"body.{field}"]


async def test_an_assessment_for_an_unknown_project_is_a_404() -> None:
    service, _ = await make_service(assets=make_assets(), model_response={})

    async with client_for(service) as client:
        response = await client.post("/api/projects/project-none/assessments", json=ASSESSMENT_BODY)

    assert response.status_code == 404
    assert_envelope(response.json(), response.headers[REQUEST_ID_HEADER], "project_not_found")


# --- POST /api/projects/{id}/plans ---------------------------------------------


async def test_generating_a_plan_returns_the_activated_version() -> None:
    service = await deterministic_service()

    async with client_for(service) as client:
        # The fixture project already owns the evidence the plan is allowed to cite.
        project_id = PROJECT_ID
        assessment = await submit_assessment(client, project_id)
        response = await client.post(
            f"/api/projects/{project_id}/plans", json={"assessment_id": assessment["id"]}
        )

    assert response.status_code == 201, response.text
    plan = response.json()
    assert plan["status"] == "active"
    assert plan["version"] == 1
    assert plan["assessment_id"] == assessment["id"]
    assert plan["project_id"] == project_id
    assert [task["stage"] for task in plan["tasks"]] == list(range(7))
    assert plan["tasks"][0]["status"] == "todo"
    assert {task["plan_id"] for task in plan["tasks"]} == {plan["id"]}
    stored = await service.repository.get_project(project_id)
    assert stored is not None and stored.active_plan_id == plan["id"]


async def test_plan_generation_uses_the_committed_handoff_and_retrieved_evidence() -> None:
    service, _ = await make_service(
        assets=make_assets(),
        model=DeterministicModelGateway(),
        context=FakeContext(handoff_text="baseline 已完成，下一步是学习率消融"),
    )

    async with client_for(service) as client:
        project_id = PROJECT_ID
        assessment = await submit_assessment(client, project_id)
        plan = (
            await client.post(
                f"/api/projects/{project_id}/plans", json={"assessment_id": assessment["id"]}
            )
        ).json()

    assert [call[0] for call in service.context.prepared] == [f"project:{project_id}"]
    # One retrieval call per stage, in stage order, asking for exactly that stage's kinds.
    assert [call[2] for call in service.engine.calls] == [
        ["operations", "note"],
        ["paper", "note"],
        ["code", "config", "operations"],
        ["code", "config"],
        ["code", "config", "experiment"],
        ["code", "experiment", "note"],
        ["note", "experiment"],
    ]
    # The fake cites exactly what the project's evidence offers, so every citation resolves.
    asset_ids = {item.id for item in make_assets()}
    assert {
        citation["asset_id"] for task in plan["tasks"] for citation in task["citations"]
    } <= asset_ids


async def test_a_plan_with_a_material_gap_is_returned_as_an_inactive_draft() -> None:
    """No experiment evidence at stage 4, so the baseline is a gap rather than a to-do."""
    evidence = [item for item in make_assets() if item.kind != "experiment"]
    service, _ = await make_service(assets=evidence, model=DeterministicModelGateway())

    async with client_for(service) as client:
        project_id = PROJECT_ID
        assessment = await submit_assessment(client, project_id)
        response = await client.post(
            f"/api/projects/{project_id}/plans", json={"assessment_id": assessment["id"]}
        )
        listed = await client.get(f"/api/projects/{project_id}/assets")

    plan = response.json()
    assert response.status_code == 201
    assert plan["status"] == "draft"
    assert [task["status"] for task in plan["tasks"]][4] == "needs_material"
    assert all(task["status"] != "todo" for task in plan["tasks"])
    # The gap is about evidence, not about the project being unusable: its assets still list.
    assert listed.status_code == 200 and len(listed.json()) == len(evidence)


@pytest.mark.parametrize(
    ("tasks", "code", "reason"),
    [
        (
            [task_payload("a", 0, "ops-1", ("b",)), task_payload("b", 1, "note-1", ("a",))],
            "plan_validation_failed",
            "dependency_cycle",
        ),
        (
            [task_payload("a", 0, "note-1", citations=[{"bogus": True}])],
            "plan_validation_failed",
            "invalid_task_payload",
        ),
        (
            [task_payload("a", 0, "ops-1", prerequisite_task_ids=123)],
            "plan_validation_failed",
            "invalid_task_payload",
        ),
        (
            [task_payload("a", 0, "ops-1", prerequisite_task_ids=True)],
            "plan_validation_failed",
            "invalid_task_payload",
        ),
        (
            [task_payload("a", 0, "ops-1", prerequisite_task_ids=None)],
            "plan_validation_failed",
            "invalid_task_payload",
        ),
    ],
    ids=["cycle", "malformed_task", "prereq_number", "prereq_boolean", "prereq_null"],
)
async def test_an_unacceptable_plan_answers_with_the_reason_and_no_model_text(
    tasks: list[dict[str, Any]], code: str, reason: str
) -> None:
    service, _ = await make_service(assets=make_assets(), model_response={"tasks": tasks})

    async with client_for(service) as client:
        project_id = await make_project(client)
        assessment = await submit_assessment(client, project_id)
        response = await client.post(
            f"/api/projects/{project_id}/plans", json={"assessment_id": assessment["id"]}
        )

    assert response.status_code == 422
    error = assert_envelope(response.json(), response.headers[REQUEST_ID_HEADER], code)
    assert error["details"]["reason"] == reason
    assert error["details"]["project_id"] == project_id
    assert "任务" not in response.text


async def test_a_plan_generated_for_an_unknown_assessment_is_a_404() -> None:
    service = await deterministic_service()

    async with client_for(service) as client:
        project_id = await make_project(client)
        response = await client.post(
            f"/api/projects/{project_id}/plans", json={"assessment_id": "assessment-none"}
        )

    assert response.status_code == 404
    error = assert_envelope(
        response.json(), response.headers[REQUEST_ID_HEADER], "assessment_not_found"
    )
    assert error["details"] == {"assessment_id": "assessment-none"}


async def test_an_assessment_of_another_project_is_not_a_plan_source() -> None:
    """One learner's answers cannot shape another project's path, and the API does not confirm
    that the foreign id exists."""
    service = await deterministic_service()

    async with client_for(service) as client:
        first = await make_project(client)
        second = await make_project(client)
        assessment = await submit_assessment(client, first)
        response = await client.post(
            f"/api/projects/{second}/plans", json={"assessment_id": assessment["id"]}
        )

    assert response.status_code == 404
    assert_envelope(response.json(), response.headers[REQUEST_ID_HEADER], "assessment_not_found")
    assert service.repository.plans == {}


async def test_an_empty_assessment_id_never_reaches_the_repository() -> None:
    service = await deterministic_service()

    async with client_for(service) as client:
        project_id = await make_project(client)
        response = await client.post(
            f"/api/projects/{project_id}/plans", json={"assessment_id": ""}
        )

    assert response.status_code == 422
    error = assert_envelope(
        response.json(), response.headers[REQUEST_ID_HEADER], "validation_failed"
    )
    assert [item["loc"] for item in error["details"]["errors"]] == ["body.assessment_id"]


async def test_a_memory_outage_while_generating_is_reported_as_a_503() -> None:
    """Reading the committed Handoff is a PowerContext call, so its outage keeps its code."""
    service = await deterministic_service(context=HandoffOutageContext())

    async with client_for(service) as client:
        project_id = await make_project(client)
        assessment = await submit_assessment(client, project_id)
        response = await client.post(
            f"/api/projects/{project_id}/plans", json={"assessment_id": assessment["id"]}
        )

    assert response.status_code == 503
    error = assert_envelope(
        response.json(), response.headers[REQUEST_ID_HEADER], "dependency_unavailable"
    )
    assert error["details"] == {"dependency": "powercontext", "project_id": project_id}
    assert service.repository.plans == {}


class HandoffOutageContext(FakeContext):
    """``prepare_context`` answers the way a down PowerContext does, with a secret in the text."""

    async def prepare_context(self, scope_id: str, query: str, max_bytes: int):
        raise PowerContextUnavailableError("token super-secret-value was rejected")


async def test_an_unexpected_store_failure_while_generating_stays_a_redacted_500(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Task 6's ruling holds: only a typed dependency error may claim 503, everything else is
    an ``internal_error`` whose body carries neither the message nor the credential."""
    service = await deterministic_service()
    service.repository = FailingAssetStore(service.repository)

    async with client_for(service) as client:
        project_id = await make_project(client)
        assessment = await submit_assessment(client, project_id)
        with caplog.at_level(logging.ERROR, logger=ERRORS_LOGGER):
            response = await client.post(
                f"/api/projects/{project_id}/plans", json={"assessment_id": assessment["id"]}
            )

    assert response.status_code == 500
    error = assert_envelope(response.json(), response.headers[REQUEST_ID_HEADER], "internal_error")
    assert error["details"] == {}
    assert "super-secret-value" not in response.text
    assert "RuntimeError" in caplog.text
    assert "super-secret-value" not in caplog.text


class FailingAssetStore:
    """A store that fails the way an unreachable seekdb does: loudly, with a secret in it."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def list_assets(self, project_id: str) -> list[Any]:
        raise RuntimeError("seekdb connection failed password=super-secret-value")


# --- GET /api/plans/{id} --------------------------------------------------------


async def test_a_generated_plan_can_be_read_back_by_id() -> None:
    service = await deterministic_service()

    async with client_for(service) as client:
        project_id = PROJECT_ID
        assessment = await submit_assessment(client, project_id)
        generated = await client.post(
            f"/api/projects/{project_id}/plans", json={"assessment_id": assessment["id"]}
        )
        reread = await client.get(f"/api/plans/{generated.json()['id']}")

    assert reread.status_code == 200
    assert reread.json() == generated.json()


async def test_an_unknown_plan_is_a_404_that_names_it() -> None:
    service = await deterministic_service()

    async with client_for(service) as client:
        response = await client.get("/api/plans/plan-none")

    assert response.status_code == 404
    error = assert_envelope(response.json(), response.headers[REQUEST_ID_HEADER], "plan_not_found")
    assert error["details"] == {"plan_id": "plan-none"}


async def test_replanning_the_same_project_grows_the_version_and_the_id_space() -> None:
    service = await deterministic_service()

    async with client_for(service) as client:
        project_id = PROJECT_ID
        assessment = await submit_assessment(client, project_id)
        first = (
            await client.post(
                f"/api/projects/{project_id}/plans", json={"assessment_id": assessment["id"]}
            )
        ).json()
        second = (
            await client.post(
                f"/api/projects/{project_id}/plans", json={"assessment_id": assessment["id"]}
            )
        ).json()

    assert (first["version"], second["version"]) == (1, 2)
    assert {task["id"] for task in first["tasks"]}.isdisjoint(
        {task["id"] for task in second["tasks"]}
    )
