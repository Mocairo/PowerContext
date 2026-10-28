import asyncio
import json

import pyseekdb
import pytest

from labrelay.config import LabRelayConfig
from labrelay.domain.models import Asset, Chunk, Citation, LearningTask
from labrelay.persistence.seekdb import SeekdbRepository, _schema_statements, _task_params
from labrelay.retrieval.seekdb import (
    SeekdbEmbeddingFunction,
    SeekdbSearchEngine,
    _collection_schema,
    _diversify,
    _parse_locator,
)


class FakeModel:
    dimension = 2

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[1.0, 0.0] for _ in texts]


class RecordingEmbedding:
    dimension = 2

    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.events.append("embed")
        if self.fail:
            raise RuntimeError("embedding unavailable")
        return [[1.0, 0.0] for _ in texts]


class RecordingCollection:
    def __init__(self, events: list[str], result: dict | None = None) -> None:
        self.events = events
        self.result = result or {}
        self.calls: list[tuple[str, dict]] = []

    def delete(self, *, where: dict) -> None:
        self.events.append("delete")
        self.calls.append(("delete", {"where": where}))

    def upsert(self, **kwargs) -> None:
        self.events.append("upsert")
        self.calls.append(("upsert", kwargs))

    def hybrid_search(self, **kwargs) -> dict:
        self.calls.append(("hybrid_search", kwargs))
        return self.result


def _engine(collection: RecordingCollection, embedding: RecordingEmbedding) -> SeekdbSearchEngine:
    engine = object.__new__(SeekdbSearchEngine)
    engine._collection = collection
    engine._embedding_function = embedding
    return engine


def _asset(status: str = "active") -> Asset:
    return Asset(
        id="asset-1",
        project_id="project-1",
        kind="code",
        title="train.py",
        uri="repo/train.py",
        checksum="abc",
        status=status,
    )


def _chunk() -> Chunk:
    return Chunk(
        id="asset-1:1",
        asset_id="asset-1",
        ordinal=0,
        content="run baseline",
        locator={"line_start": 1},
        token_count=2,
    )


def _search_result(asset_ids: list[str]) -> dict:
    return {
        "ids": [[f"chunk-{index}" for index in range(len(asset_ids))]],
        "metadatas": [
            [
                {
                    "asset_id": asset_id,
                    "kind": "code",
                    "title": f"{asset_id}.py",
                    "uri": f"repo/{asset_id}.py",
                    "version": "",
                    "locator_json": json.dumps({"line_start": 1}),
                    "excerpt": "baseline",
                }
                for asset_id in asset_ids
            ]
        ],
        "documents": [["baseline" for _ in asset_ids]],
        "distances": [[0.25 for _ in asset_ids]],
    }


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


def test_schema_statements_cover_core_tables_and_task_order() -> None:
    statements = _schema_statements()

    assert len(statements) == 6
    assert all(statement.startswith("CREATE TABLE IF NOT EXISTS") for statement in statements)
    sql = "\n".join(statements)
    assert "position INT NOT NULL DEFAULT 0" in sql
    assert "INDEX idx_tasks_plan_position (plan_id, position)" in sql


def test_repository_from_config_and_database_identifier_validation() -> None:
    config = LabRelayConfig(
        seekdb_host="db.internal",
        seekdb_port=2881,
        seekdb_database="labrelay_test",
        seekdb_user="root",
        seekdb_password="secret",
    )
    repository = SeekdbRepository.from_config(config)

    assert repository._host == "db.internal"
    assert repository._database == "labrelay_test"
    assert repository._user == "root"
    assert repository._password == "secret"

    with pytest.raises(ValueError, match="Invalid seekdb database name"):
        SeekdbRepository(
            host="db.internal",
            port=2881,
            database="labrelay-test",
            user="root",
            password="secret",
        )


def test_task_params_round_trip_keeps_payload_and_position() -> None:
    citation = Citation(
        asset_id="asset-1",
        kind="code",
        title="train.py",
        uri="repo/train.py",
        version="abc123",
        locator={"line_start": 1, "line_end": 3},
    )
    task = LearningTask(
        id="task-1",
        project_id="project-1",
        plan_id="plan-1",
        stage=3,
        title="Read the training entrypoint",
        objective="Trace the baseline call chain",
        citations=[citation],
        completion_criteria="Explain the call chain",
    )

    params = _task_params(task, position=2)

    assert params[9] == 2
    assert LearningTask.model_validate_json(params[10]) == task


def test_upsert_embeds_before_replacing_active_chunks() -> None:
    events: list[str] = []
    collection = RecordingCollection(events)
    engine = _engine(collection, RecordingEmbedding(events))

    engine._upsert_sync([_chunk()], [_asset()])

    assert events == ["embed", "delete", "upsert"]
    assert collection.calls[1][1]["embeddings"] == [[1.0, 0.0]]


def test_upsert_keeps_old_chunks_when_embedding_fails() -> None:
    events: list[str] = []
    collection = RecordingCollection(events)
    engine = _engine(collection, RecordingEmbedding(events, fail=True))

    with pytest.raises(RuntimeError, match="embedding unavailable"):
        engine._upsert_sync([_chunk()], [_asset()])

    assert events == ["embed"]
    assert collection.calls == []


def test_upsert_evicts_restricted_and_chunkless_assets_without_embedding() -> None:
    for status, chunks in (("restricted", []), ("failed", []), ("active", [])):
        events: list[str] = []
        collection = RecordingCollection(events)
        engine = _engine(collection, RecordingEmbedding(events))

        engine._upsert_sync(chunks, [_asset(status=status)])

        assert events == ["delete"]
        assert collection.calls == [("delete", {"where": {"asset_id": "asset-1"}})]


def test_upsert_skips_chunks_whose_asset_is_missing() -> None:
    events: list[str] = []
    collection = RecordingCollection(events)
    engine = _engine(collection, RecordingEmbedding(events))

    engine._upsert_sync([_chunk()], [])

    assert events == []
    assert collection.calls == []


def test_search_requests_extra_candidates_then_diversifies_and_slices() -> None:
    events: list[str] = []
    collection = RecordingCollection(events, _search_result(["asset-1"] * 4 + ["asset-2"]))
    engine = _engine(collection, RecordingEmbedding(events))

    hits = engine._search_sync("project-1", "baseline", ["code"], 4)

    assert [hit.citation.asset_id for hit in hits] == [
        "asset-1",
        "asset-1",
        "asset-1",
        "asset-2",
    ]
    assert hits[0].score == 0.25
    kwargs = collection.calls[0][1]
    assert kwargs["n_results"] == 16
    assert kwargs["query"]["where"] == {
        "project_id": "project-1",
        "status": "active",
        "kind": {"$in": ["code"]},
    }
    assert kwargs["knn"]["where"] == kwargs["query"]["where"]


def test_search_empty_query_does_not_touch_the_collection() -> None:
    events: list[str] = []
    collection = RecordingCollection(events)
    engine = _engine(collection, RecordingEmbedding(events))

    assert asyncio.run(engine.search("project-1", "   ", [], 5)) == []
    assert collection.calls == []


def test_convert_builds_citations_and_caps_chunks_per_asset() -> None:
    def metadata(asset_id: str, chunk: int) -> dict[str, str]:
        return {
            "asset_id": asset_id,
            "kind": "code",
            "title": f"{asset_id}.py",
            "uri": f"repo/{asset_id}.py",
            "version": "",
            "locator_json": json.dumps({"line_start": chunk}),
            "excerpt": "baseline",
        }

    result = {
        "ids": [["c1", "c2", "c3", "c4", "c5"]],
        "metadatas": [
            [
                metadata("asset-1", 1),
                metadata("asset-1", 2),
                metadata("asset-1", 3),
                metadata("asset-1", 4),
                metadata("asset-2", 1),
            ]
        ],
        "documents": [["baseline one", "baseline two", "baseline three", "baseline four", "other"]],
        "distances": [[0.25, 0.25, 0.25, 0.25, 0.25]],
    }

    hits = _diversify(SeekdbSearchEngine._convert(result))

    assert [hit.citation.asset_id for hit in hits] == [
        "asset-1",
        "asset-1",
        "asset-1",
        "asset-2",
    ]
    assert hits[0].citation.version is None
    assert hits[0].citation.locator == {"line_start": 1}
    assert hits[0].score == 0.25


def test_convert_skips_rows_without_asset_metadata() -> None:
    result = {
        "ids": [["c1"]],
        "metadatas": [[{"kind": "code"}]],
        "documents": [["baseline"]],
        "distances": [[0.0]],
    }

    assert SeekdbSearchEngine._convert(result) == []


def test_parse_locator_discards_non_scalar_values() -> None:
    assert _parse_locator('{"line_start": 1, "score": 0.5, "note": "x"}') == {
        "line_start": 1,
        "note": "x",
    }
    assert _parse_locator("not json") == {}
    assert _parse_locator({"line_start": 1}) == {"line_start": 1}
    assert _parse_locator(None) == {}
