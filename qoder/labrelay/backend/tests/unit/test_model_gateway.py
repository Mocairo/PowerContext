"""Task 8: the OpenAI-compatible model gateway and its deterministic stand-in.

Tasks 7, 10, 12 and 13 put either of these objects behind the same ``ModelGateway``
port, so the tests target the contract rather than the happy path: generation and
embedding may live on different base URLs with different keys, an HTTP failure has to
surface instead of being parsed as if it succeeded, the configured vector width has to
reach Task 7, and the deterministic fake has to build the seven-stage task list from the
stages the planner hands it — never from a canned answer.
"""

import inspect
import json
from typing import Any

import httpx
import pytest

from labrelay.config import LabRelayConfig
from labrelay.domain.models import LearningTask
from labrelay.domain.ports import ModelGateway
from labrelay.model.fake import DeterministicModelGateway
from labrelay.model.openai_compat import OpenAICompatModelGateway

GENERATION_URL = "https://gen.test/v1"
EMBEDDING_URL = "https://embed.test/v1"

#: Mirrors ``stage_required_kinds`` in the planner Task 10 builds; the fake must read the
#: requirement out of the payload rather than hard-code a stage table of its own.
STAGE_REQUIRED_KINDS: dict[int, list[str]] = {
    0: ["operations", "note"],
    1: ["paper", "note"],
    2: ["code", "config", "operations"],
    3: ["code", "config"],
    4: ["code", "config", "experiment"],
    5: ["code", "experiment", "note"],
    6: ["note", "experiment"],
}


class RecordingTransport:
    """``httpx.MockTransport`` handler: replays path-keyed replies and keeps the requests."""

    def __init__(self, replies: dict[str, tuple[int, Any]]) -> None:
        self._replies = replies
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for path, (status, payload) in self._replies.items():
            if request.url.path.endswith(path):
                return httpx.Response(status, json=payload)
        raise AssertionError(f"unexpected request to {request.url}")

    @property
    def last(self) -> httpx.Request:
        assert self.requests, "the gateway sent no request"
        return self.requests[-1]

    def body(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


def make_gateway(
    transport: RecordingTransport,
    *,
    base_url: str = GENERATION_URL,
    api_key: str = "gen-key",
    embedding_base_url: str | None = EMBEDDING_URL,
    embedding_api_key: str | None = "emb-key",
    embedding_dimensions: int | None = None,
) -> OpenAICompatModelGateway:
    return OpenAICompatModelGateway(
        base_url=base_url,
        api_key=api_key,
        generation_model="generator",
        embedding_model="embedder",
        embedding_base_url=embedding_base_url,
        embedding_api_key=embedding_api_key,
        embedding_dimensions=embedding_dimensions,
        transport=httpx.MockTransport(transport),
    )


def completion(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"content": content}}]}


def citation(asset_id: str, kind: str) -> dict[str, Any]:
    return {
        "asset_id": asset_id,
        "kind": kind,
        "title": f"{asset_id}.{kind}",
        "uri": f"repo/{asset_id}",
        "version": "commit-1",
        "locator": {"line_start": 1, "line_end": 20},
        "excerpt": "证据片段",
    }


def stages(evidence_by_stage: dict[int, list[str]] | None = None) -> list[dict[str, Any]]:
    """The per-stage payload shape ``planning/generator.py`` builds from search hits.

    ``evidence_by_stage`` overrides coverage for the listed stages only; every other
    stage keeps its required kinds fully covered.
    """
    overrides = evidence_by_stage or {}
    return [
        {
            "id": stage,
            "required_evidence": STAGE_REQUIRED_KINDS[stage],
            "evidence": [
                {"citation": citation(f"asset-{stage}-{kind}", kind), "score": 1.0}
                for kind in overrides.get(stage, STAGE_REQUIRED_KINDS[stage])
            ],
        }
        for stage in range(7)
    ]


def plan_payload(evidence_by_stage: dict[int, list[str]] | None = None) -> dict[str, Any]:
    return {
        "project": {"id": "project-1", "title": "基线复现"},
        "handoff": None,
        "assessment": {"id": "assessment-1"},
        "stages": stages(evidence_by_stage),
    }


def _protocol_methods(protocol: type[ModelGateway]) -> set[str]:
    return {
        name
        for base in protocol.__mro__
        for name, member in vars(base).items()
        if not name.startswith("_") and callable(member)
    }


# --- embedding channel ----------------------------------------------------------


def test_embed_posts_to_the_embedding_url_with_the_embedding_key() -> None:
    transport = RecordingTransport(
        {"/embeddings": (200, {"data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}]})}
    )

    vectors = make_gateway(transport).embed(["run the baseline", "读论文"])

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert str(transport.last.url) == "https://embed.test/v1/embeddings"
    assert transport.last.headers["Authorization"] == "Bearer emb-key"
    assert transport.body() == {"model": "embedder", "input": ["run the baseline", "读论文"]}


def test_embed_asks_for_the_configured_dimension_only_when_one_is_set() -> None:
    fixed = RecordingTransport({"/embeddings": (200, {"data": [{"embedding": [0.0] * 8}]})})
    assert make_gateway(fixed, embedding_dimensions=8).embed(["x"]) == [[0.0] * 8]

    unfixed = RecordingTransport({"/embeddings": (200, {"data": [{"embedding": [0.0]}]})})
    assert make_gateway(unfixed).embed(["x"]) == [[0.0]]

    assert fixed.body()["dimensions"] == 8
    assert "dimensions" not in unfixed.body()


def test_embed_skips_the_round_trip_for_an_empty_batch() -> None:
    transport = RecordingTransport({"/embeddings": (200, {"data": []})})

    assert make_gateway(transport).embed([]) == []
    assert transport.requests == []


def test_embed_surfaces_an_error_response_instead_of_parsing_it() -> None:
    transport = RecordingTransport(
        {"/embeddings": (400, {"error": {"message": "dimension mismatch with stored vectors"}})}
    )

    with pytest.raises(httpx.HTTPStatusError) as raised:
        make_gateway(transport).embed(["hello"])

    assert raised.value.response.status_code == 400
    assert len(transport.requests) == 1


# --- generation channel ---------------------------------------------------------


async def test_generate_json_posts_to_the_generation_url_with_the_generation_key() -> None:
    transport = RecordingTransport({"/chat/completions": (200, completion('{"answer":"ok"}'))})

    result = await make_gateway(transport).generate_json("只返回 JSON", {"task": "test"})

    assert result == {"answer": "ok"}
    assert str(transport.last.url) == "https://gen.test/v1/chat/completions"
    assert transport.last.headers["Authorization"] == "Bearer gen-key"
    body = transport.body()
    assert body["model"] == "generator"
    assert body["response_format"] == {"type": "json_object"}
    assert body["temperature"] == 0
    assert body["messages"][0] == {"role": "system", "content": "只返回 JSON"}
    assert json.loads(body["messages"][1]["content"]) == {"task": "test"}


async def test_generate_json_round_trips_a_full_plan_payload() -> None:
    """Nested Chinese evidence must survive the request and the reply untouched."""
    payload = plan_payload()
    transport = RecordingTransport(
        {"/chat/completions": (200, completion(json.dumps(payload, ensure_ascii=False)))}
    )

    result = await make_gateway(transport).generate_json("生成学习路径", payload)

    assert result == payload
    # ``transport.body()`` is already a parsed dict
    assert transport.body()["messages"][1]["content"] == json.dumps(payload, ensure_ascii=False)


async def test_generate_json_surfaces_an_error_response_instead_of_parsing_it() -> None:
    transport = RecordingTransport(
        {"/chat/completions": (503, {"error": {"message": "model overloaded"}})}
    )

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await make_gateway(transport).generate_json("system", {"task": "test"})

    assert raised.value.response.status_code == 503


# --- configuration plumbing -----------------------------------------------------


async def test_gateway_falls_back_to_one_url_and_key_for_both_channels() -> None:
    transport = RecordingTransport(
        {
            "/embeddings": (200, {"data": [{"embedding": [0.5]}]}),
            "/chat/completions": (200, completion('{"answer":"ok"}')),
        }
    )
    # one base URL and one key configured: the embedding channel reuses the generation pair
    gateway = make_gateway(
        transport,
        base_url="https://one.test/v1/",
        embedding_base_url=None,
        embedding_api_key=None,
    )

    assert await gateway.generate_json("system", {"task": "t"}) == {"answer": "ok"}
    assert gateway.embed(["hi"]) == [[0.5]]
    # the trailing slash on the configured base URL never doubles up in the request path
    assert [str(request.url) for request in transport.requests] == [
        "https://one.test/v1/chat/completions",
        "https://one.test/v1/embeddings",
    ]
    assert {request.headers["Authorization"] for request in transport.requests} == {
        "Bearer gen-key"
    }


def test_dimension_reports_the_configured_embedding_width() -> None:
    transport = RecordingTransport({})

    assert make_gateway(transport, embedding_dimensions=1536).dimension == 1536
    assert make_gateway(transport).dimension is None


def test_from_config_maps_generation_and_embedding_settings_separately() -> None:
    config = LabRelayConfig(
        generation_base_url=GENERATION_URL,
        generation_api_key="gen-key",
        generation_model="generator",
        embedding_base_url=EMBEDDING_URL,
        embedding_api_key="emb-key",
        embedding_model="embedder",
    )

    gateway = OpenAICompatModelGateway.from_config(config)

    assert (gateway.base_url, gateway.api_key, gateway.generation_model) == (
        GENERATION_URL,
        "gen-key",
        "generator",
    )
    assert (gateway.embedding_base_url, gateway.embedding_api_key, gateway.embedding_model) == (
        EMBEDDING_URL,
        "emb-key",
        "embedder",
    )
    assert gateway.dimension is None


def test_from_config_falls_back_to_the_embedding_service_for_generation() -> None:
    gateway = OpenAICompatModelGateway.from_config(
        LabRelayConfig(
            embedding_base_url=EMBEDDING_URL,
            embedding_api_key="emb-key",
            embedding_model="embedder",
        )
    )

    assert gateway.base_url == EMBEDDING_URL
    assert gateway.api_key == "emb-key"
    assert gateway.generation_model == ""


def test_from_config_needs_at_least_one_base_url() -> None:
    with pytest.raises(ValueError, match="GENERATION_BASE_URL"):
        OpenAICompatModelGateway.from_config(LabRelayConfig())


# --- deterministic gateway ------------------------------------------------------


def test_deterministic_embed_returns_one_vector_of_the_configured_width() -> None:
    gateway = DeterministicModelGateway(dimension=3)

    assert gateway.dimension == 3
    assert gateway.embed(["复现 baseline 的命令", "训练日志", "阅读论文笔记"]) == [
        [1.0, 1.0, 1.0],
        [1.0, 1.0, 1.0],
        [0.5, 0.5, 0.5],
    ]


async def test_deterministic_gateway_builds_the_seven_stages_from_the_payload() -> None:
    response = await DeterministicModelGateway().generate_json(
        "生成 LabRelay 学习路径 JSON", plan_payload()
    )
    tasks = response["tasks"]

    assert [task["stage"] for task in tasks] == list(range(7))
    assert [task["id"] for task in tasks] == [f"stage-{stage}" for stage in range(7)]
    assert [task["status"] for task in tasks] == ["locked"] * 7
    assert tasks[0]["prerequisite_task_ids"] == []
    assert tasks[3]["prerequisite_task_ids"] == ["stage-2"]
    assert [entry["kind"] for entry in tasks[4]["citations"]] == [
        "code",
        "config",
        "experiment",
    ]
    assert [entry["kind"] for entry in tasks[1]["citations"]] == ["paper"]
    for task in tasks:
        assert LearningTask.model_validate(
            {**task, "project_id": "project-1", "plan_id": "plan-1"}
        ).stage == task["stage"]


async def test_deterministic_gateway_needs_only_the_stages_key() -> None:
    """No canned ``expected_plan`` key: the plan is derived from the evidence supplied."""
    response = await DeterministicModelGateway().generate_json("", {"stages": stages()})

    assert len(response["tasks"]) == 7


async def test_stage_four_without_every_required_kind_needs_material() -> None:
    response = await DeterministicModelGateway().generate_json(
        "system", plan_payload({4: ["code"]})
    )
    tasks = response["tasks"]

    assert [task["stage"] for task in tasks if task["status"] == "needs_material"] == [4]
    assert tasks[4]["task_type"] == "citation_required"
    assert [entry["kind"] for entry in tasks[4]["citations"]] == ["code"]


async def test_stage_without_any_evidence_falls_back_to_self_assessment() -> None:
    response = await DeterministicModelGateway().generate_json(
        "system", plan_payload({2: []})
    )
    task = response["tasks"][2]

    assert task["task_type"] == "self_assessment"
    assert task["citations"] == []
    assert task["status"] == "locked"
    assert LearningTask.model_validate(
        {**task, "project_id": "project-1", "plan_id": "plan-1"}
    ).task_type == "self_assessment"


async def test_deterministic_gateway_rejects_a_payload_without_stages() -> None:
    with pytest.raises(ValueError, match="stages"):
        await DeterministicModelGateway().generate_json("system", {"project": {"id": "p"}})


# --- port conformance -----------------------------------------------------------


@pytest.mark.parametrize(
    "gateway_cls",
    [OpenAICompatModelGateway, DeterministicModelGateway],
    ids=["openai_compat", "deterministic"],
)
def test_both_gateways_match_the_model_port_exactly(gateway_cls: type) -> None:
    names = _protocol_methods(ModelGateway)
    assert names == {"generate_json", "embed"}
    for name in names:
        method = getattr(gateway_cls, name, None)
        assert method is not None, f"{gateway_cls.__name__} is missing {name}"
        assert list(inspect.signature(method).parameters) == list(
            inspect.signature(getattr(ModelGateway, name)).parameters
        ), f"{name} drifted from the port signature"
        assert inspect.iscoroutinefunction(method) is inspect.iscoroutinefunction(
            getattr(ModelGateway, name)
        ), f"{name} must keep the port's sync/async shape"


def test_the_package_exposes_both_gateways() -> None:
    """Task 12 imports them from ``labrelay.model``."""
    import labrelay.model as model_package

    assert model_package.OpenAICompatModelGateway is OpenAICompatModelGateway
    assert model_package.DeterministicModelGateway is DeterministicModelGateway
