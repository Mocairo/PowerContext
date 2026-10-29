"""Task 8: the OpenAI-compatible model gateway and its deterministic stand-in.

Tasks 7, 10, 12 and 13 put either of these objects behind the same ``ModelGateway``
port, so the tests target the contract rather than the happy path: generation and
embedding may live on different base URLs with different keys, an HTTP failure has to
surface instead of being parsed as if it succeeded, a 2xx reply that does not match the
OpenAI shape has to become one clear ``ValueError`` that leaks neither the body nor the
key, a channel has to refuse to fire when its own configuration is missing, a transport
only has to be usable by the channel it actually implements, the configured vector width
has to reach Task 7, and the deterministic fake has to build the seven-stage task list
from the stages the planner hands it — never from a canned answer, and with task ids that
stay unique per plan version.
"""

import inspect
import json
from itertools import pairwise
from types import NoneType
from typing import Any, get_args, get_type_hints

import httpx
import pytest

from labrelay.config import EmbeddingConfig, LabRelayConfig
from labrelay.domain.models import LearningTask
from labrelay.domain.ports import ModelGateway
from labrelay.model.fake import DeterministicModelGateway
from labrelay.model.openai_compat import OpenAICompatModelGateway

GENERATION_URL = "https://gen.test/v1"
EMBEDDING_URL = "https://embed.test/v1"
GENERATION_KEY = "gen-key"
EMBEDDING_KEY = "emb-key"

#: Stands in for anything secret-ish a provider might echo back (response bodies can
#: carry prompts, keys or stack traces); it must never reach an exception message.
SECRET_BODY_MARKER = "SECRET-FROM-PROVIDER-BODY"

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
                if isinstance(payload, RawBody):
                    return httpx.Response(status, text=payload.text)
                return httpx.Response(status, json=payload)
        raise AssertionError(f"unexpected request to {request.url}")

    @property
    def last(self) -> httpx.Request:
        assert self.requests, "the gateway sent no request"
        return self.requests[-1]

    def body(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


class RawBody:
    """A reply body that is *not* JSON, for the malformed-response tests."""

    def __init__(self, text: str) -> None:
        self.text = text


class ExplodingTransport:
    """Handler that raises a non-HTTP error, to prove the gateway does not swallow it."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        raise self.error


class AsyncOnlyTransport(httpx.AsyncBaseTransport):
    """A transport that can only serve the async channel (no ``handle_request``)."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append(request)
        return httpx.Response(200, json=self.payload)


class SyncOnlyTransport(httpx.BaseTransport):
    """A transport that can only serve the sync channel (no ``handle_async_request``)."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        return httpx.Response(200, json=self.payload)


def make_gateway(
    transport: RecordingTransport
    | ExplodingTransport
    | httpx.BaseTransport
    | httpx.AsyncBaseTransport,
    *,
    base_url: str = GENERATION_URL,
    api_key: str = GENERATION_KEY,
    generation_model: str = "generator",
    embedding_model: str = "embedder",
    embedding_base_url: str | None = EMBEDDING_URL,
    embedding_api_key: str | None = EMBEDDING_KEY,
    embedding_dimensions: int | None = None,
) -> OpenAICompatModelGateway:
    """Passes a transport object through as-is; plain handlers become a ``MockTransport``."""
    resolved = (
        transport
        if isinstance(transport, (httpx.BaseTransport, httpx.AsyncBaseTransport))
        else httpx.MockTransport(transport)
    )
    return OpenAICompatModelGateway(
        base_url=base_url,
        api_key=api_key,
        generation_model=generation_model,
        embedding_model=embedding_model,
        embedding_base_url=embedding_base_url,
        embedding_api_key=embedding_api_key,
        embedding_dimensions=embedding_dimensions,
        transport=resolved,
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
    stage keeps its required kinds fully covered. An unknown stage key means the test
    meant to override a stage the planner does not emit, so it fails loudly here rather
    than silently producing a plan that tests nothing.
    """
    overrides = evidence_by_stage or {}
    unknown = sorted(set(overrides) - set(STAGE_REQUIRED_KINDS))
    if unknown:
        raise ValueError(
            f"unknown stage keys {unknown} in the evidence override; "
            f"valid stages are {sorted(STAGE_REQUIRED_KINDS)}"
        )
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


def plan_payload(
    evidence_by_stage: dict[int, list[str]] | None = None,
    *,
    assessment_id: str = "assessment-1",
    plan_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "project": {"id": "project-1", "title": "基线复现"},
        "handoff": None,
        "assessment": {"id": assessment_id},
        "stages": stages(evidence_by_stage),
    }
    if plan_id is not None:
        payload["plan_id"] = plan_id
    return payload


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


# --- malformed 2xx responses (I-2) ------------------------------------------------

#: ``(status, body)`` replies plus the fragment the raised error must carry. Every reply
#: is a *successful* HTTP 200, so a broken provider surfaces as one clear ValueError
#: naming the channel and the endpoint instead of a ``KeyError``/``IndexError``.
MALFORMED_GENERATION: list[tuple[tuple[int, Any], str]] = [
    ((200, RawBody(f"<html>proxy blew up {SECRET_BODY_MARKER}</html>")), "valid JSON"),
    ((200, [SECRET_BODY_MARKER]), "JSON object"),
    ((200, {"results": [SECRET_BODY_MARKER]}), "'choices'"),
    ((200, {"choices": []}), "'choices'"),
    ((200, {"choices": ["", SECRET_BODY_MARKER]}), "object"),
    ((200, {"choices": [{"text": SECRET_BODY_MARKER}]}), "'message'"),
    ((200, {"choices": [{"message": {"reasoning": SECRET_BODY_MARKER}}]}), "content"),
    ((200, {"choices": [{"message": {"content": 7}}]}), "content"),
    ((200, completion(f"not json {SECRET_BODY_MARKER}")), "valid JSON"),
    ((200, completion(f'["{SECRET_BODY_MARKER}", "ok"]')), "JSON object"),
]

#: ``(texts, reply, fragment)``: the batch guard fires before the per-item ones, so the
#: item-level cases send a single input.
MALFORMED_EMBEDDING: list[tuple[list[str], tuple[int, Any], str]] = [
    (["a"], (200, RawBody(f"<html>proxy blew up {SECRET_BODY_MARKER}</html>")), "valid JSON"),
    (["a"], (200, [SECRET_BODY_MARKER]), "JSON object"),
    (["a"], (200, {"vectors": [[0.1]]}), "'data'"),
    (["a"], (200, {"data": {"embedding": [0.1]}}), "'data'"),
    (["a"], (200, {"data": [[0.1]]}), "object"),
    (["a"], (200, {"data": [{"vector": [0.1]}]}), "'embedding'"),
    (["a"], (200, {"data": [{SECRET_BODY_MARKER: [0.1]}]}), "'embedding'"),
    (["a"], (200, {"data": [{"embedding": "0.1,0.2"}]}), "numbers"),
    (["a"], (200, {"data": [{"embedding": [0.1, None]}]}), "numbers"),
    (["a", "b"], (200, {"data": []}), "batch"),
    (["a"], (200, {"data": [{"embedding": [0.1]}, {"embedding": [0.2]}]}), "batch"),
]


@pytest.mark.parametrize(
    ("reply", "expected"),
    MALFORMED_GENERATION,
    ids=[index for index, _ in enumerate(MALFORMED_GENERATION)],
)
async def test_generate_json_rejects_a_malformed_success_response(
    reply: Any, expected: str
) -> None:
    transport = RecordingTransport({"/chat/completions": reply})

    with pytest.raises(ValueError, match=expected) as raised:
        await make_gateway(transport).generate_json("system", {"task": "test"})

    message = str(raised.value)
    assert "generation" in message and "/chat/completions" in message
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    ("texts", "reply", "expected"),
    MALFORMED_EMBEDDING,
    ids=[index for index, _ in enumerate(MALFORMED_EMBEDDING)],
)
def test_embed_rejects_a_malformed_success_response(
    texts: list[str], reply: Any, expected: str
) -> None:
    transport = RecordingTransport({"/embeddings": reply})

    with pytest.raises(ValueError, match=expected) as raised:
        make_gateway(transport).embed(texts)

    message = str(raised.value)
    assert "embedding" in message and "/embeddings" in message
    assert len(transport.requests) == 1


def test_embed_checks_the_vector_width_against_the_configured_dimension() -> None:
    transport = RecordingTransport({"/embeddings": (200, {"data": [{"embedding": [0.1, 0.2]}]})})

    with pytest.raises(ValueError, match="width") as raised:
        make_gateway(transport, embedding_dimensions=8).embed(["a"])

    assert "8" in str(raised.value) and "2" in str(raised.value)


async def test_a_malformed_generation_response_never_leaks_the_body_or_the_key() -> None:
    """The provider's body can carry the prompt and the answer; the error text may not."""
    leaked = f"the prompt body says {SECRET_BODY_MARKER}"
    transport = RecordingTransport({"/chat/completions": (200, completion(leaked))})
    gateway = make_gateway(transport, api_key="sk-secret-generation-key")

    with pytest.raises(ValueError) as raised:
        await gateway.generate_json("system", {"task": "test"})

    message = str(raised.value)
    assert "generation" in message and "/chat/completions" in message
    assert "sk-secret" not in message
    assert SECRET_BODY_MARKER not in message
    assert "prompt body" not in message


def test_a_malformed_embedding_response_never_leaks_the_body_or_the_key() -> None:
    """Two inputs but one vector back: a batch error, with the body left out."""
    transport = RecordingTransport(
        {"/embeddings": (200, {"data": [{"embedding": [0.1], "note": SECRET_BODY_MARKER}]})}
    )
    gateway = make_gateway(transport, embedding_api_key="sk-secret-embedding-key")

    with pytest.raises(ValueError) as raised:
        gateway.embed(["a", "b"])

    message = str(raised.value)
    assert "embedding" in message and "/embeddings" in message
    assert "sk-secret" not in message
    assert SECRET_BODY_MARKER not in message


def test_an_unrelated_transport_error_is_not_wrapped_in_a_value_error() -> None:
    """I-2 must not become a blanket ``except Exception``: only the JSON shape is checked,
    everything the transport raises travels on untouched."""
    broken = make_gateway(ExplodingTransport(RuntimeError("provider exploded")))

    with pytest.raises(RuntimeError, match="provider exploded"):
        broken.embed(["a"])

    with pytest.raises(httpx.HTTPStatusError):
        make_gateway(
            RecordingTransport({"/embeddings": (429, {"error": {"message": "slow down"}})})
        ).embed(["a"])


async def test_an_unrelated_transport_error_is_not_wrapped_for_generation() -> None:
    broken = make_gateway(ExplodingTransport(RuntimeError("provider exploded")))

    with pytest.raises(RuntimeError, match="provider exploded"):
        await broken.generate_json("system", {"task": "t"})

    with pytest.raises(httpx.HTTPStatusError):
        await make_gateway(
            RecordingTransport({"/chat/completions": (429, {"error": {"message": "slow down"}})})
        ).generate_json("system", {"task": "t"})


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


async def test_from_config_falls_back_to_the_embedding_service_for_generation() -> None:
    """I-4: a generation-less deployment still constructs, but the request must fail
    fast on the missing model instead of POSTing ``"model": ""`` to the endpoint."""
    gateway = OpenAICompatModelGateway.from_config(
        LabRelayConfig(
            embedding_base_url=EMBEDDING_URL,
            embedding_api_key="emb-key",
            embedding_model="embedder",
        )
    )

    assert gateway.base_url == EMBEDDING_URL
    assert gateway.api_key == "emb-key"
    with pytest.raises(ValueError, match="GENERATION_MODEL") as raised:
        await gateway.generate_json("system", {"task": "t"})
    assert "set GENERATION_MODEL" in str(raised.value)


def test_from_config_needs_at_least_one_base_url() -> None:
    with pytest.raises(ValueError, match="GENERATION_BASE_URL"):
        OpenAICompatModelGateway.from_config(LabRelayConfig())


# --- transport shape (I-3) --------------------------------------------------------


async def test_generate_json_accepts_an_async_only_transport() -> None:
    """Real async transports (``httpx.AsyncBaseTransport`` subclasses) implement no
    ``handle_request``: the generation channel must work with exactly that shape."""
    transport = AsyncOnlyTransport(completion('{"answer":"ok"}'))
    assert not isinstance(transport, httpx.BaseTransport)

    result = await make_gateway(transport).generate_json("system", {"task": "t"})

    assert result == {"answer": "ok"}
    assert len(transport.requests) == 1


def test_embed_accepts_a_sync_only_transport() -> None:
    transport = SyncOnlyTransport({"data": [{"embedding": [0.1, 0.2]}]})
    assert not isinstance(transport, httpx.AsyncBaseTransport)

    assert make_gateway(transport).embed(["hello"]) == [[0.1, 0.2]]
    assert len(transport.requests) == 1


async def test_a_one_sided_transport_is_rejected_by_the_channel_it_cannot_serve() -> None:
    """The mismatch is reported by the gateway, not by an ``AttributeError`` inside httpx."""
    with pytest.raises(TypeError, match="embedding") as raised:
        make_gateway(AsyncOnlyTransport(completion("{}"))).embed(["hello"])
    assert "handle_request" in str(raised.value)

    with pytest.raises(TypeError, match="generation") as raised:
        await make_gateway(SyncOnlyTransport({"data": []})).generate_json("system", {"t": 1})
    assert "handle_async_request" in str(raised.value)


def test_the_transport_annotation_covers_both_shapes() -> None:
    members = get_args(get_type_hints(OpenAICompatModelGateway.__init__)["transport"])

    assert httpx.BaseTransport in members
    assert httpx.AsyncBaseTransport in members
    assert NoneType in members


# --- fail-fast per channel (I-4) --------------------------------------------------

BROKEN_GENERATION: list[dict[str, Any]] = [
    {"base_url": ""},
    {"base_url": "   "},
    {"api_key": ""},
    {"generation_model": ""},
]

BROKEN_EMBEDDING: list[dict[str, Any]] = [
    {"base_url": "", "embedding_base_url": ""},
    {"embedding_api_key": "", "api_key": ""},
    {"embedding_model": ""},
    {"embedding_dimensions": 0},
    {"embedding_dimensions": -8},
]


@pytest.mark.parametrize("broken", BROKEN_GENERATION, ids=range(len(BROKEN_GENERATION)))
async def test_generate_json_fails_fast_on_its_own_channel(broken: dict[str, Any]) -> None:
    transport = RecordingTransport({"/chat/completions": (200, completion('{"answer":"ok"}'))})

    with pytest.raises(ValueError, match="GENERATION_") as raised:
        await make_gateway(transport, **broken).generate_json("system", {"task": "t"})

    assert "set GENERATION_" in str(raised.value)
    assert transport.requests == []


@pytest.mark.parametrize("broken", BROKEN_EMBEDDING, ids=range(len(BROKEN_EMBEDDING)))
def test_embed_fails_fast_on_its_own_channel(broken: dict[str, Any]) -> None:
    reply = {"data": [{"embedding": [0.1]}]}
    transport = RecordingTransport({"/embeddings": (200, reply)})

    with pytest.raises(ValueError, match="EMBEDDING_") as raised:
        make_gateway(transport, **broken).embed(["hello"])

    assert "set EMBEDDING_" in str(raised.value)
    assert transport.requests == []


async def test_one_usable_channel_works_even_when_the_other_is_misconfigured() -> None:
    """The checks are per channel: an unwired generation model must not break embedding."""
    transport = RecordingTransport(
        {
            "/embeddings": (200, {"data": [{"embedding": [0.1]}]}),
            "/chat/completions": (200, completion('{"answer":"ok"}')),
        }
    )
    # the generation model is missing, yet the embedding channel must work
    gateway = make_gateway(transport, generation_model="")

    assert gateway.embed(["hello"]) == [[0.1]]
    assert len(transport.requests) == 1


def test_from_config_carries_a_broken_dimension_into_the_embedding_check() -> None:
    """``EMBEDDING_DIMENSIONS=0`` is a config mistake, not a vector the provider owes us."""
    gateway = OpenAICompatModelGateway.from_config(
        LabRelayConfig(
            embedding_base_url=EMBEDDING_URL,
            embedding_api_key="emb-key",
            embedding_model="embedder",
            embedding=EmbeddingConfig(dimensions=0),
        )
    )

    with pytest.raises(ValueError, match="EMBEDDING_DIMENSIONS"):
        gateway.embed(["hello"])


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
    # task ids are namespaced by the assessment they were generated for, so two plan
    # versions of one project never write onto the same primary key
    assert [task["id"] for task in tasks] == [f"assessment-1-stage-{stage}" for stage in range(7)]
    assert [task["status"] for task in tasks] == ["locked"] * 7
    assert tasks[0]["prerequisite_task_ids"] == []
    assert tasks[3]["prerequisite_task_ids"] == ["assessment-1-stage-2"]
    assert [entry["kind"] for entry in tasks[4]["citations"]] == [
        "code",
        "config",
        "experiment",
    ]
    assert [entry["kind"] for entry in tasks[1]["citations"]] == ["paper"]
    for task in tasks:
        assert (
            LearningTask.model_validate(
                {**task, "project_id": "project-1", "plan_id": "plan-1"}
            ).stage
            == task["stage"]
        )


async def test_task_ids_never_collide_across_assessment_ids() -> None:
    """I-1: re-planning produced the same ``stage-0`` ids, so the second plan overwrote
    the first (and Task 11's prerequisite chain pointed at the wrong version)."""
    gateway = DeterministicModelGateway()

    first = await gateway.generate_json("system", plan_payload(assessment_id="assessment-a"))
    second = await gateway.generate_json("system", plan_payload(assessment_id="assessment-b"))

    first_ids = [task["id"] for task in first["tasks"]]
    second_ids = [task["id"] for task in second["tasks"]]
    assert set(first_ids).isdisjoint(second_ids)
    assert len(set(first_ids + second_ids)) == 14
    # every id still validates, and each plan chains to *its own* previous stage
    for plan in (first, second):
        ids = {task["id"] for task in plan["tasks"]}
        assert plan["tasks"][0]["prerequisite_task_ids"] == []
        for previous, current in pairwise(plan["tasks"]):
            assert current["prerequisite_task_ids"] == [previous["id"]]
            assert set(current["prerequisite_task_ids"]) <= ids
            assert (
                LearningTask.model_validate(
                    {**current, "project_id": "project-1", "plan_id": "plan-1"}
                ).id
                == current["id"]
            )


async def test_task_ids_are_namespaced_by_the_plan_being_generated() -> None:
    """Task 10's carry-forward: one assessment, two plan versions, two disjoint id spaces.

    Replanning reuses the stored assessment payload, so the assessment id alone is not enough to
    keep ``learning_task.id`` unique across versions — the ``plan_id`` the planner sends is.
    """
    gateway = DeterministicModelGateway()
    shared = plan_payload(assessment_id="assessment-a")
    first = await gateway.generate_json("system", {**shared, "plan_id": "plan-one"})
    second = await gateway.generate_json("system", {**shared, "plan_id": "plan-two"})
    again = await gateway.generate_json("system", {**shared, "plan_id": "plan-one"})

    assert [task["id"] for task in first["tasks"]] == [
        f"plan-one-stage-{stage}" for stage in range(7)
    ]
    assert {task["id"] for task in first["tasks"]}.isdisjoint(
        {task["id"] for task in second["tasks"]}
    )
    # Same plan id, same ids: the fake stays deterministic inside one version.
    assert [task["id"] for task in again["tasks"]] == [task["id"] for task in first["tasks"]]
    # Each version still chains to its own previous stage, never to the other version's.
    for plan in (first, second):
        for previous, current in pairwise(plan["tasks"]):
            assert current["prerequisite_task_ids"] == [previous["id"]]


async def test_a_long_plan_id_is_hashed_into_the_column_width() -> None:
    """The ``plan_id`` namespace obeys the same VARCHAR(64) limit as the assessment one."""
    response = await DeterministicModelGateway().generate_json(
        "system", plan_payload(plan_id="p" * 200)
    )

    assert all(len(task["id"]) <= 64 for task in response["tasks"])
    assert response["tasks"][3]["prerequisite_task_ids"] == [response["tasks"][2]["id"]]


async def test_task_ids_are_stable_for_the_same_assessment() -> None:
    """Determinism is the point of the fake: the same payload yields the same ids."""
    gateway = DeterministicModelGateway()
    payload = plan_payload(assessment_id="assessment-a")

    first = await gateway.generate_json("system", payload)
    second = await gateway.generate_json("system", payload)

    assert [task["id"] for task in first["tasks"]] == [task["id"] for task in second["tasks"]]


async def test_task_ids_without_an_assessment_fall_back_to_a_payload_digest() -> None:
    """Planner payloads without ``assessment.id`` still get non-colliding ids."""
    gateway = DeterministicModelGateway()
    bare = {"stages": stages()}
    other = {"stages": stages({3: ["code"]})}

    first = await gateway.generate_json("system", bare)
    again = await gateway.generate_json("system", dict(bare))
    second = await gateway.generate_json("system", other)

    first_ids = [task["id"] for task in first["tasks"]]
    assert first_ids == [task["id"] for task in again["tasks"]]
    assert set(first_ids).isdisjoint({task["id"] for task in second["tasks"]})
    assert all(task["id"].startswith("payload-") for task in first["tasks"])
    assert all(len(task["id"]) <= 64 for task in first["tasks"])


async def test_task_ids_stay_inside_the_database_column_width() -> None:
    """``learning_task.id`` is a VARCHAR(64): a long assessment id must not overflow it."""
    response = await DeterministicModelGateway().generate_json(
        "system", plan_payload(assessment_id="a" * 200)
    )
    tasks = response["tasks"]

    assert all(len(task["id"]) <= 64 for task in tasks)
    assert tasks[3]["prerequisite_task_ids"] == [tasks[2]["id"]]


def test_plan_helper_rejects_an_unknown_stage_override() -> None:
    """A typo'd override used to be silently ignored, so the stage under test stayed green."""
    with pytest.raises(ValueError, match="unknown stage"):
        stages({42: ["code"]})


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
    response = await DeterministicModelGateway().generate_json("system", plan_payload({2: []}))
    task = response["tasks"][2]

    assert task["task_type"] == "self_assessment"
    assert task["citations"] == []
    assert task["status"] == "locked"
    assert (
        LearningTask.model_validate(
            {**task, "project_id": "project-1", "plan_id": "plan-1"}
        ).task_type
        == "self_assessment"
    )


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


def question_payload(evidence: list[dict[str, object]]) -> dict[str, object]:
    return {"question": "baseline 之前要注意什么？", "project_context": None, "evidence": evidence}


def hit(asset_id: str, title: str, excerpt: str) -> dict[str, object]:
    return {
        "citation": {
            "asset_id": asset_id,
            "kind": "note",
            "title": title,
            "uri": f"notes/{title}",
            "version": None,
            "locator": {"line_start": 3},
            "excerpt": excerpt,
        },
        "score": 0.9,
    }


async def test_deterministic_gateway_answers_from_the_retrieved_excerpt() -> None:
    """With no model configured, the stand-in quotes evidence instead of failing the request."""
    response = await DeterministicModelGateway().generate_json(
        "system", question_payload([hit("note-1", "handoff.md", "先清理历史缓存再训练。")])
    )

    assert response["sufficient"] is True
    assert response["used_asset_ids"] == ["note-1"]
    assert "先清理历史缓存再训练。" in response["answer"]
    assert "handoff.md" in response["answer"]


async def test_deterministic_gateway_refuses_a_question_without_evidence() -> None:
    response = await DeterministicModelGateway().generate_json("system", question_payload([]))

    assert response["sufficient"] is False
    assert response["used_asset_ids"] == []
    assert "无法" in response["answer"]


async def test_deterministic_gateway_rejects_a_payload_with_neither_stages_nor_question() -> None:
    with pytest.raises(ValueError, match="stages"):
        await DeterministicModelGateway().generate_json("system", {"nothing": True})
