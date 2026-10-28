"""Offline tests for the seekdb retrieval engine: connection isolation and ranking."""

import asyncio
import json
from pathlib import Path

import pyseekdb
import pytest
import yaml

from labrelay.config import LabRelayConfig
from labrelay.domain.models import Asset, Chunk
from labrelay.retrieval import seekdb
from labrelay.retrieval.seekdb import (
    MAX_CANDIDATES,
    MAX_CHUNKS_PER_ASSET,
    MAX_EXCERPT_CHARACTERS,
    SeekdbEmbeddingFunction,
    SeekdbSearchEngine,
    _collection_schema,
    _convert,
    _diversify,
    _parse_locator,
    _relevance_from_rank,
)

RETRIEVAL_CONFIG = Path(__file__).resolve().parents[3] / "configs" / "retrieval.yaml"

DEFAULT_EXISTING_IDS = ["asset-1:lines:1-2"]
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 10


class FakeModel:
    dimension = 2

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[1.0, 0.0] for _ in texts]


class RecordingModel:
    """A ``ModelGateway`` double that records embedding calls and can simulate an outage."""

    dimension = 2

    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.events.append("embed")
        if self.fail:
            raise RuntimeError("embedding unavailable")
        return [[1.0, 0.0] for _ in texts]


class FakeCollection:
    """Stand-in for ``pyseekdb.Collection`` that records every SDK call."""

    def __init__(self, events: list[str], *, existing: list[str] | None = None) -> None:
        self.events = events
        self.existing = list(existing) if existing is not None else list(DEFAULT_EXISTING_IDS)
        self.calls: list[tuple[str, dict]] = []
        self.result: dict = {}
        self.search_error: Exception | None = None
        self.unreachable = False

    def get(self, *, where: dict | None = None, include: list[str] | None = None) -> dict:
        self.events.append("get")
        self.calls.append(("get", {"where": where, "include": include}))
        return {"ids": list(self.existing)}

    def delete(self, *, ids=None, where: dict | None = None) -> None:
        self.events.append("delete")
        self.calls.append(("delete", {"ids": ids, "where": where}))
        if ids is not None:
            removed = set(ids)
            self.existing = [chunk_id for chunk_id in self.existing if chunk_id not in removed]
        elif where is not None:
            self.existing = []

    def upsert(self, **kwargs) -> None:
        self.events.append("upsert")
        self.calls.append(("upsert", kwargs))
        self.existing = sorted({*self.existing, *kwargs["ids"]})

    def hybrid_search(self, **kwargs) -> dict:
        self.events.append("search")
        self.calls.append(("hybrid_search", kwargs))
        if self.search_error is not None:
            raise self.search_error
        return self.result

    def count(self) -> int:
        self.events.append("count")
        return len(self.existing)


class FakeServer:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def _cleanup(self) -> None:
        self.events.append("close")


class FakeClient:
    def __init__(self, events: list[str], collection: FakeCollection, *, exists: bool) -> None:
        self.events = events
        self.collection = collection
        self.exists = exists
        self._server = FakeServer(events)
        self.opened_collections = 0
        self.created_collections = 0

    def has_collection(self, name: str) -> bool:
        assert name == "labrelay_chunks"
        return self.exists

    def get_collection(self, name: str, *, embedding_function) -> FakeCollection:
        self.opened_collections += 1
        self.embedding_function = embedding_function
        return self.collection

    def create_collection(self, *, name: str, schema) -> FakeCollection:
        self.created_collections += 1
        self.schema = schema
        return self.collection


class FakeClientFactory:
    """Engine-level client factory double: counts live clients per operation."""

    def __init__(self, trace: list[str], collection: FakeCollection, *, exists: bool) -> None:
        self.trace = trace
        self.collection = collection
        self.exists = exists
        self.clients: list[FakeClient] = []
        self.kwargs: list[dict] = []

    def __call__(self, **kwargs) -> FakeClient:
        self.trace.append("client")
        if self.collection.unreachable:
            raise ConnectionError("seekdb is down")
        self.kwargs.append(kwargs)
        client = FakeClient(self.trace, self.collection, exists=self.exists)
        self.clients.append(client)
        return client


def _factory(
    *, exists: bool = True, unreachable: bool = False, existing: list[str] | None = None
) -> tuple[FakeClientFactory, FakeCollection, list[str]]:
    trace: list[str] = []
    collection = FakeCollection(trace, existing=existing)
    collection.unreachable = unreachable
    return FakeClientFactory(trace, collection, exists=exists), collection, trace


def _engine(factory: FakeClientFactory, embedding, **overrides) -> SeekdbSearchEngine:
    settings = {
        "host": "db.internal",
        "port": 2881,
        "database": "labrelay",
        "user": "root",
        "password": "secret",
        "collection_name": "labrelay_chunks",
        "model": embedding,
        "dimensions": 2,
        "tenant": "labrelay",
        "client_factory": factory,
    }
    settings.update(overrides)
    return SeekdbSearchEngine(**settings)


def _asset(status: str = "active", asset_id: str = "asset-1") -> Asset:
    return Asset(
        id=asset_id,
        project_id="project-1",
        kind="code",
        title="train.py",
        uri="repo/train.py",
        checksum="abc",
        status=status,
    )


def _chunk(chunk_id: str = "asset-1:lines:1-2", asset_id: str = "asset-1") -> Chunk:
    return Chunk(
        id=chunk_id,
        asset_id=asset_id,
        ordinal=0,
        content="run baseline",
        locator={"line_start": 1},
        token_count=2,
    )


def _metadata(
    asset_id: str = "asset-1",
    *,
    chunk: int = 1,
    project_id: str = "project-1",
    status: str = "active",
    kind: str = "code",
) -> dict:
    return {
        "asset_id": asset_id,
        "project_id": project_id,
        "status": status,
        "kind": kind,
        "title": f"{asset_id}.py",
        "uri": f"repo/{asset_id}.py",
        "version": "",
        "locator_json": json.dumps({"line_start": chunk}),
        "excerpt": "baseline",
    }


def _search_result(metadatas: list[dict], distances: list[float]) -> dict:
    return {
        "ids": [[f"chunk-{index}" for index in range(len(metadatas))]],
        "metadatas": [metadatas],
        "documents": [[row["excerpt"] for row in metadatas]],
        "distances": [distances],
    }


# --------------------------------------------------------------------------- C1


def test_constructor_never_touches_the_database() -> None:
    class ExplodingFactory:
        def __call__(self, **kwargs):
            raise AssertionError("the constructor must not open a connection")

    engine = _engine(ExplodingFactory(), RecordingModel([]))

    assert engine is not None


def test_operations_open_and_close_their_own_client() -> None:
    factory, collection, trace = _factory(existing=["asset-1:lines:1-2"])
    engine = _engine(factory, RecordingModel(trace))

    asyncio.run(engine.upsert_chunks([_chunk()], [_asset()]))
    collection.result = _search_result([_metadata()], [0.9])
    asyncio.run(engine.search("project-1", "baseline", ["code"], 2))

    assert len(factory.clients) == 2
    assert trace.count("client") == 2
    # every operation opens its own collection and closes its own connection
    assert [client.opened_collections for client in factory.clients] == [1, 1]
    assert [client is not collection for client in factory.clients] == [True, True]
    assert trace.count("close") == 2


def test_client_is_built_with_tenant_and_bounded_timeouts() -> None:
    factory, _, trace = _factory()
    engine = _engine(factory, RecordingModel(trace))

    asyncio.run(engine.health_check())

    assert factory.kwargs[0] == {
        "host": "db.internal",
        "port": 2881,
        "tenant": "labrelay",
        "database": "labrelay",
        "user": "root",
        "password": "secret",
        "connect_timeout": CONNECT_TIMEOUT,
        "read_timeout": READ_TIMEOUT,
        "write_timeout": READ_TIMEOUT,
    }


def test_initialize_reuses_an_existing_collection() -> None:
    factory, _, trace = _factory()
    engine = _engine(factory, RecordingModel(trace))

    asyncio.run(engine.initialize())

    assert factory.clients[0].opened_collections == 1
    assert factory.clients[0].created_collections == 0
    assert trace.count("close") == 1


def test_initialize_creates_a_missing_collection_with_the_ngram_schema() -> None:
    factory, _, trace = _factory(exists=False)
    engine = _engine(factory, RecordingModel(trace))

    asyncio.run(engine.initialize())

    client = factory.clients[0]
    assert client.created_collections == 1
    assert client.schema.fulltext_index.analyzer == "ngram"
    assert client.schema.vector_index.hnsw.dimension == 2


def test_health_check_reports_unreachable_database_as_false() -> None:
    failing, _, trace = _factory(unreachable=True)
    engine = _engine(failing, RecordingModel(trace))

    assert asyncio.run(engine.health_check()) is False


def test_health_check_returns_true_when_the_collection_answers() -> None:
    factory, _, trace = _factory()
    engine = _engine(factory, RecordingModel(trace))

    assert asyncio.run(engine.health_check()) is True
    assert "count" in trace


def test_closed_engine_refuses_further_operations() -> None:
    factory, _, trace = _factory()
    engine = _engine(factory, RecordingModel(trace))

    asyncio.run(engine.close())
    asyncio.run(engine.close())

    with pytest.raises(RuntimeError, match="closed"):
        asyncio.run(engine.search("project-1", "baseline", ["code"], 3))
    assert trace.count("client") == 0


# --------------------------------------------------------------------------- I2


def test_upsert_writes_new_chunks_before_deleting_stale_ids() -> None:
    factory, collection, trace = _factory(existing=["old-1", "old-2", "asset-1:lines:1-2"])
    engine = _engine(factory, RecordingModel(trace))

    asyncio.run(engine.upsert_chunks([_chunk()], [_asset()]))

    assert trace[: trace.index("delete")] == ["client", "embed", "get", "upsert"]
    delete_call = next(call for name, call in collection.calls if name == "delete")
    assert delete_call == {"ids": ["old-1", "old-2"], "where": None}
    assert collection.existing == ["asset-1:lines:1-2"]


def test_upsert_keeps_a_chunk_id_that_reappears_in_the_new_batch() -> None:
    factory, collection, trace = _factory(existing=["asset-1:lines:1-2"])
    engine = _engine(factory, RecordingModel(trace))

    asyncio.run(engine.upsert_chunks([_chunk()], [_asset()]))

    assert [name for name, _ in collection.calls if name == "delete"] == []
    assert collection.existing == ["asset-1:lines:1-2"]


def test_upsert_stale_deletes_are_scoped_to_the_active_assets_of_this_call() -> None:
    factory, collection, trace = _factory(existing=[])
    engine = _engine(factory, RecordingModel(trace))

    asyncio.run(
        engine.upsert_chunks(
            [_chunk("asset-1:lines:1-2"), _chunk("asset-2:lines:1-2", "asset-2")],
            [_asset(), _asset(asset_id="asset-2")],
        )
    )

    gets = [call for name, call in collection.calls if name == "get"]
    assert [call["where"] for call in gets] == [{"asset_id": "asset-1"}, {"asset_id": "asset-2"}]
    assert [call["include"] for call in gets] == [[], []]


def test_upsert_keeps_old_chunks_when_embedding_fails() -> None:
    factory, collection, trace = _factory(existing=["old-1"])
    engine = _engine(factory, RecordingModel(trace, fail=True))

    with pytest.raises(RuntimeError, match="embedding unavailable"):
        asyncio.run(engine.upsert_chunks([_chunk()], [_asset()]))

    assert [name for name, _ in collection.calls] == []
    assert collection.existing == ["old-1"]


def test_upsert_evicts_restricted_and_chunkless_assets_without_embedding() -> None:
    for status, chunks in (("restricted", []), ("failed", []), ("active", [])):
        factory, collection, trace = _factory(existing=["old-1"])
        engine = _engine(factory, RecordingModel(trace))

        asyncio.run(engine.upsert_chunks(chunks, [_asset(status=status)]))

        assert [name for name, _ in collection.calls] == ["delete"]
        assert collection.calls[0][1] == {"ids": None, "where": {"asset_id": "asset-1"}}


def test_upsert_skips_chunks_whose_asset_is_missing() -> None:
    factory, collection, trace = _factory(existing=["old-1"])
    engine = _engine(factory, RecordingModel(trace))

    asyncio.run(engine.upsert_chunks([_chunk()], []))

    assert [name for name, _ in collection.calls] == []
    assert collection.existing == ["old-1"]


# --------------------------------------------------------------------------- I1


def test_search_drops_hits_that_do_not_belong_to_the_project() -> None:
    factory, collection, trace = _factory()
    engine = _engine(factory, RecordingModel(trace))
    collection.result = _search_result(
        [_metadata("asset-1"), _metadata("asset-2", project_id="project-2")],
        [0.9, 0.8],
    )

    hits = asyncio.run(engine.search("project-1", "baseline", ["code"], 5))

    assert [hit.citation.asset_id for hit in hits] == ["asset-1"]


def test_search_drops_hits_that_are_no_longer_active() -> None:
    factory, collection, trace = _factory()
    engine = _engine(factory, RecordingModel(trace))
    collection.result = _search_result(
        [_metadata("asset-1"), _metadata("asset-2", status="restricted")],
        [0.9, 0.8],
    )

    hits = asyncio.run(engine.search("project-1", "baseline", ["code"], 5))

    assert [hit.citation.asset_id for hit in hits] == ["asset-1"]


def test_search_drops_hits_whose_kind_was_not_requested() -> None:
    factory, collection, trace = _factory()
    engine = _engine(factory, RecordingModel(trace))
    collection.result = _search_result(
        [_metadata("asset-1"), _metadata("asset-2", kind="paper")],
        [0.9, 0.8],
    )

    hits = asyncio.run(engine.search("project-1", "baseline", ["code"], 5))

    assert [hit.citation.asset_id for hit in hits] == ["asset-1"]


def test_search_requests_the_project_filter_in_both_legs() -> None:
    factory, collection, trace = _factory()
    engine = _engine(factory, RecordingModel(trace))
    collection.result = _search_result([_metadata()], [0.9])

    asyncio.run(engine.search("project-1", "baseline", ["code"], 4))

    kwargs = collection.calls[0][1]
    assert kwargs["n_results"] == 4 * 4
    assert kwargs["query"]["where"] == {
        "project_id": "project-1",
        "status": "active",
        "kind": {"$in": ["code"]},
    }
    assert kwargs["knn"]["where"] == kwargs["query"]["where"]
    assert kwargs["knn"]["n_results"] == kwargs["n_results"]


def test_search_never_asks_for_more_than_the_configured_candidates() -> None:
    factory, collection, trace = _factory()
    engine = _engine(factory, RecordingModel(trace))
    collection.result = _search_result([_metadata()], [0.9])

    asyncio.run(engine.search("project-1", "baseline", ["code"], 40))

    assert collection.calls[0][1]["n_results"] == MAX_CANDIDATES


def test_search_empty_query_does_not_open_a_client() -> None:
    factory, _, trace = _factory()
    engine = _engine(factory, RecordingModel(trace))

    assert asyncio.run(engine.search("project-1", "   ", [], 5)) == []
    assert trace == []


# --------------------------------------------------------------------------- I3


def test_scores_never_come_from_the_distance_channel() -> None:
    ascending = _convert(
        _search_result([_metadata("asset-1"), _metadata("asset-2")], [0.1, 0.9]),
        project_id="project-1",
        allowed_kinds=[],
    )
    descending = _convert(
        _search_result([_metadata("asset-1"), _metadata("asset-2")], [0.9, 0.1]),
        project_id="project-1",
        allowed_kinds=[],
    )

    assert [hit.citation.asset_id for hit in ascending] == [
        hit.citation.asset_id for hit in descending
    ]
    assert [hit.score for hit in ascending] == [hit.score for hit in descending]


def test_scores_are_reversed_distance_proof_and_sorted_descending() -> None:
    hits = _convert(
        _search_result(
            [_metadata("asset-1"), _metadata("asset-2"), _metadata("asset-3")],
            [0.05, 0.9, 0.4],
        ),
        project_id="project-1",
        allowed_kinds=[],
    )

    assert [hit.score for hit in hits] == sorted((hit.score for hit in hits), reverse=True)
    assert hits[0].score == pytest.approx(_relevance_from_rank(0))
    assert hits[0].score > hits[-1].score
    assert all(0.0 < hit.score <= 1.0 for hit in hits)


def test_relevance_from_rank_is_the_reciprocal_rank_fusion_default() -> None:
    assert _relevance_from_rank(0) == pytest.approx(1 / 61)
    assert _relevance_from_rank(10) == pytest.approx(1 / 71)


def test_search_diversifies_and_slices_after_sorting() -> None:
    factory, collection, trace = _factory()
    engine = _engine(factory, RecordingModel(trace))
    collection.result = _search_result(
        [_metadata("asset-1") for _ in range(4)] + [_metadata("asset-2")],
        [0.05, 0.9, 0.4, 0.7, 0.2],
    )

    hits = asyncio.run(engine.search("project-1", "baseline", ["code"], 4))

    assert [hit.citation.asset_id for hit in hits] == [
        "asset-1",
        "asset-1",
        "asset-1",
        "asset-2",
    ]


def test_convert_builds_citations_from_rank_order() -> None:
    hits = _convert(
        _search_result([_metadata("asset-1", chunk=2), _metadata("asset-2")], [0.3, 0.1]),
        project_id="project-1",
        allowed_kinds=["code"],
    )

    assert hits[0].citation.version is None
    assert hits[0].citation.locator == {"line_start": 2}
    assert hits[0].citation.excerpt == "baseline"
    assert hits[0].score == pytest.approx(_relevance_from_rank(0))


def test_convert_skips_rows_without_asset_metadata() -> None:
    result = {
        "ids": [["c1"]],
        "metadatas": [[{"kind": "code"}]],
        "documents": [["baseline"]],
        "distances": [[0.0]],
    }

    assert _convert(result, project_id="project-1", allowed_kinds=[]) == []


def test_diversify_caps_chunks_per_asset() -> None:
    hits = _convert(
        _search_result([_metadata("asset-1", chunk=index) for index in range(1, 6)], [0.5] * 5),
        project_id="project-1",
        allowed_kinds=[],
    )

    assert [hit.citation.asset_id for hit in _diversify(hits)] == ["asset-1"] * MAX_CHUNKS_PER_ASSET


# ------------------------------------------------------------- embedding bridge


def test_embedding_function_bridges_model_gateway() -> None:
    model = FakeModel()
    function = SeekdbEmbeddingFunction(model)

    assert function.dimension == 2
    assert function("baseline") == [[1.0, 0.0]]
    assert function(["a", "b"]) == [[1.0, 0.0], [1.0, 0.0]]
    assert model.calls == [["baseline"], ["a", "b"]]


def test_embedding_function_requires_dimension_or_config_fallback() -> None:
    class NoDimension:
        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0] for _ in texts]

    function = SeekdbEmbeddingFunction(NoDimension())

    with pytest.raises(ValueError, match="Embedding dimension"):
        _ = function.dimension
    assert SeekdbEmbeddingFunction(NoDimension(), dimensions=4).dimension == 4


def test_embedding_function_is_rebound_rather_than_persisted() -> None:
    function = SeekdbEmbeddingFunction(FakeModel())

    assert function.get_config() is NotImplemented
    assert pyseekdb.EmbeddingFunction.support_persistence(function) is False
    with pytest.raises(NotImplementedError):
        SeekdbEmbeddingFunction.build_from_config({})


def test_collection_schema_uses_ngram_for_mixed_code_and_chinese() -> None:
    schema = _collection_schema(SeekdbEmbeddingFunction(FakeModel()))

    assert schema.fulltext_index.analyzer == "ngram"
    assert schema.vector_index.hnsw.dimension == 2


def test_from_config_passes_tenant_and_dimensions() -> None:
    config = LabRelayConfig(
        seekdb_host="db.internal",
        seekdb_port=2881,
        seekdb_database="labrelay",
        seekdb_user="root",
        seekdb_password="secret",
        seekdb_tenant="labrelay_tenant",
        embedding={"dimensions": 8},
    )
    factory, _, _ = _factory()
    original = seekdb.pyseekdb.Client
    seekdb.pyseekdb.Client = factory
    try:
        engine = SeekdbSearchEngine.from_config(config, FakeModel())
        assert engine._dimension == 8
        assert asyncio.run(engine.health_check()) is True
    finally:
        seekdb.pyseekdb.Client = original

    assert factory.kwargs[0]["tenant"] == "labrelay_tenant"
    assert factory.kwargs[0]["database"] == "labrelay"


# ------------------------------------------------------- config consistency


def test_seekdb_engine_constants_match_retrieval_yaml() -> None:
    """The engine caps and trims exactly what the shipped retrieval config declares."""
    config = yaml.safe_load(RETRIEVAL_CONFIG.read_text(encoding="utf-8"))["retrieval"]

    assert MAX_CHUNKS_PER_ASSET == config["grouping"]["max_chunks_per_asset"]
    assert MAX_EXCERPT_CHARACTERS == config["citation"]["max_excerpt_characters"]
    assert MAX_CANDIDATES == config["full_text"]["candidate_limit"]
    assert MAX_CANDIDATES == config["vector"]["candidate_limit"]
    assert config["grouping"]["preserve_asset_diversity"] is True


def test_parse_locator_discards_non_scalar_values() -> None:
    assert _parse_locator('{"line_start": 1, "score": 0.5, "note": "x"}') == {
        "line_start": 1,
        "note": "x",
    }
    assert _parse_locator("not json") == {}
    assert _parse_locator({"line_start": 1}) == {"line_start": 1}
    assert _parse_locator(None) == {}
