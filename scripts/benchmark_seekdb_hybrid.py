# Copyright (c) 2026 OceanBase.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software distributed
# under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
# CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.

"""Measure real same-database searches; synthetic embeddings are not quality acceptance.

Run with PYTHONPATH=src and the pinned competition Python. Qrels, if supplied,
are a standard BEIR TSV used only by this offline measurement script. Nothing
in the product loads labels, sample IDs, or benchmark files.
"""

from __future__ import annotations

# These assertions are the benchmark's measurement and integrity gates.
# ruff: noqa: S101
import argparse
import asyncio
import csv
import hashlib
import json
import math
import random
import statistics
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Any
from unittest.mock import patch

from sqlalchemy import Engine, event

from powercontext.builtin.artifacts.memory import EmbeddingProfile, MemoryEntryInput
from powercontext.builtin.inference import EmbeddingResult
from powercontext.builtin.persistence.memory_index import CompositeMemoryIndex
from powercontext.builtin.persistence.seekdb import SeekDBConfig
from powercontext.builtin.persistence.seekdb.memory_index import SeekDBMemoryIndex
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_contexts

PROFILE = EmbeddingProfile(
    profile_id="hybrid-public-protocol-probe",
    model="offline-sha256-seeded-positive-random",
    dimension=384,
    distance="l2",
    normalization="unit",
)


class _Embedding:
    profile = PROFILE

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        vectors = []
        for text in texts:
            generator = random.Random(hashlib.sha256(text.encode()).digest())  # noqa: S311
            values = tuple(generator.random() for _ in range(PROFILE.dimension))
            norm = math.sqrt(sum(value * value for value in values))
            vectors.append(tuple(value / norm for value in values))
        return EmbeddingResult(vectors=tuple(vectors))


@contextmanager
def _candidates() -> Iterator[list[tuple[str, Any]]]:
    statements = []

    def record(_connection, _cursor, statement, parameters, _context, _executemany):
        if "AGAINST" in statement.upper() or "l2_distance" in statement.lower():
            statements.append((statement, parameters))

    event.listen(Engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(Engine, "before_cursor_execute", record)


def _quality(ids: list[str], relevant: set[str]) -> dict[str, float]:
    return {
        "recall_at_10": len(set(ids) & relevant) / len(relevant),
        "mrr_at_10": next((1 / rank for rank, doc_id in enumerate(ids, 1) if doc_id in relevant), 0.0),
    }


async def measure(sample_path: Path, qrels_path: Path | None, repeats: int) -> dict[str, Any]:  # noqa: C901
    sample_bytes = await asyncio.to_thread(sample_path.read_bytes)
    qrels_bytes = None if qrels_path is None else await asyncio.to_thread(qrels_path.read_bytes)
    sample = json.loads(sample_bytes)
    qrels: dict[str, set[str]] = {}
    if qrels_bytes is not None:
        for row in csv.DictReader(qrels_bytes.decode().splitlines(), delimiter="\t"):
            if int(row["score"]) > 0:
                qrels.setdefault(row["query-id"], set()).add(row["corpus-id"])
    documents = {doc["text"]: doc["id"] for doc in sample["documents"]}
    results, sql, plans = [], {}, {}
    db_path = Path(tempfile.mkdtemp(prefix="pc-hybrid-bench-")) / "db"
    async with open_builtin_contexts(
        BuiltinConfig(database=SeekDBConfig(path=db_path)), embedding_model=_Embedding()
    ) as contexts:
        for scope_id in ("comparison-a", "comparison-b", "comparison-c"):
            memory = (await contexts.get(scope_id)).artifacts.memory
            head = await memory.remember(
                memory=None,
                mode="append",
                entries=tuple(MemoryEntryInput(kind="fact", text=body) for body in documents),
            )
            assert head is not None
            entries = {entry.entry_id: entry for entry in await memory.entries(head)}
            for query in sample["queries"]:

                async def search(candidate: bool, memory=memory, head=head, query=query, entries=entries):
                    implementation = SeekDBMemoryIndex.search if candidate else CompositeMemoryIndex.search
                    with patch.object(SeekDBMemoryIndex, "search", implementation), _candidates() as statements:
                        started = time.perf_counter()
                        found = await memory.search(query["text"], memories=(head,), mode="hybrid", limit=10)
                        elapsed = (time.perf_counter() - started) * 1000
                    assert len(statements) == (1 if candidate else 2)
                    identities = []
                    doc_ids = []
                    for hit in found.hits:
                        assert hit.memory_ref == head.as_ref()
                        exact = entries[hit.entry_id]
                        assert hit.entry_version_id == exact.entry_version_id and hit.text == exact.text
                        assert hit.matched_by and set(hit.matched_by) <= {"fts", "vector"}
                        identities.append((
                            hit.memory_ref,
                            hit.entry_id,
                            hit.entry_version_id,
                            hit.text,
                            hit.matched_by,
                        ))
                        doc_ids.append(documents[hit.text])
                    assert len(doc_ids) == len(set(doc_ids))
                    label = "single_statement" if candidate else "frozen_two_statements"
                    if label not in sql:
                        sql[label] = statements
                        async with contexts.database.transaction() as connection:
                            plans[label] = [
                                "\n".join(
                                    str(row[0])
                                    for row in (
                                        await connection.exec_driver_sql("EXPLAIN " + statement, parameters)
                                    ).all()
                                )
                                for statement, parameters in statements
                            ]
                    return identities, doc_ids, elapsed

                # One warm-up per path in each scope/query, excluded from timings.
                await search(True)
                await search(False)
                first_identity = None
                for round_index in range(repeats):
                    if round_index % 2:
                        baseline = await search(False)
                        candidate = await search(True)
                    else:
                        candidate = await search(True)
                        baseline = await search(False)
                    if first_identity is None:
                        first_identity = candidate[0]
                    assert candidate[0] == first_identity
                    row = {
                        "scope": scope_id,
                        "query_id": query["id"],
                        "round": round_index,
                        "candidate_ms": candidate[2],
                        "baseline_ms": baseline[2],
                        "candidate_doc_ids": candidate[1],
                        "baseline_doc_ids": baseline[1],
                        "complete_tuple_equal": candidate[0] == baseline[0],
                        "candidate_statement_count": 1,
                        "baseline_statement_count": 2,
                    }
                    if relevant := qrels.get(query["id"]):
                        row["candidate_quality"] = _quality(candidate[1], relevant)
                        row["baseline_quality"] = _quality(baseline[1], relevant)
                    results.append(row)
    assert "TEXT RETRIEVAL SCAN" in plans["single_statement"][0]
    assert "VECTOR INDEX" in plans["single_statement"][0]
    measured = [row for row in results if "candidate_quality" in row]
    return {
        "dataset": str(sample_path),
        "dataset_sha256": hashlib.sha256(sample_bytes).hexdigest(),
        "qrels": None if qrels_path is None else str(qrels_path),
        "qrels_sha256": None if qrels_bytes is None else hashlib.sha256(qrels_bytes).hexdigest(),
        "pylibseekdb_version": version("pylibseekdb"),
        "profile": PROFILE.model_dump(),
        "scope_count": 3,
        "unique_queries": len(sample["queries"]),
        "documents_per_scope": len(documents),
        "measurement_count": len(results),
        "p50_latency_ms": statistics.median(row["candidate_ms"] for row in results),
        "baseline_p50_latency_ms": statistics.median(row["baseline_ms"] for row in results),
        "complete_tuple_equal_ratio": sum(row["complete_tuple_equal"] for row in results) / len(results),
        "quality_measurement_count": len(measured),
        "quality_query_ids": sorted({row["query_id"] for row in measured}),
        "mean_evidence_recall_at_10": (
            statistics.mean(row["candidate_quality"]["recall_at_10"] for row in measured) if measured else None
        ),
        "mean_mrr_at_10": statistics.mean(row["candidate_quality"]["mrr_at_10"] for row in measured)
        if measured
        else None,
        "per_query_quality_non_regressing": all(
            row["candidate_quality"][metric] >= row["baseline_quality"][metric]
            for row in measured
            for metric in ("recall_at_10", "mrr_at_10")
        )
        if measured
        else None,
        "limitation": "Public sample only; synthetic non-semantic vectors; repeated queries are not the official 173-document/20-query workload.",
        "statements": sql,
        "explain": plans,
        "measurements": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--qrels", type=Path)
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.repeats < 1:
        parser.error("--repeats must be positive")
    report = asyncio.run(measure(arguments.sample, arguments.qrels, arguments.repeats))
    arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
    print(
        json.dumps(
            {key: value for key, value in report.items() if key not in {"statements", "explain", "measurements"}},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
