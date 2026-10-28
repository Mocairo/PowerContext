import json

import pyseekdb
import pytest

from labrelay.config import LabRelayConfig
from labrelay.domain.models import Citation, LearningTask
from labrelay.persistence.seekdb import SeekdbRepository, _schema_statements, _task_params
from labrelay.retrieval.seekdb import (
    SeekdbEmbeddingFunction,
    SeekdbSearchEngine,
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


def test_embedding_function_bridges_model_gateway() -> None:
    model = FakeModel()
    function = SeekdbEmbeddingFunction(model)

    assert function.dimension == 2
    assert function("baseline") == [[1.0, 0.0]]
    assert function(["a", "b"]) == [[1.0, 0.0], [1.0, 0.0]]
    assert model.calls == [["baseline"], ["a", "b"]]


def test_embedding_function_requires_dimension() -> None:
    class NoDimension:
        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0] for _ in texts]

    function = SeekdbEmbeddingFunction(NoDimension())

    with pytest.raises(ValueError, match="Embedding dimension"):
        _ = function.dimension


def test_embedding_function_is_rebound_rather_than_persisted() -> None:
    function = SeekdbEmbeddingFunction(FakeModel())

    assert function.get_config() is NotImplemented
    assert pyseekdb.EmbeddingFunction.support_persistence(function) is False
    with pytest.raises(NotImplementedError):
        SeekdbEmbeddingFunction.build_from_config({})


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
        "distances": [[0.0, 0.0, 0.0, 0.0, 0.0]],
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
    assert hits[0].score == 1.0


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
