# Copyright (c) 2026 OceanBase.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software distributed
# under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
# CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.

"""Exercise candidate budgets and filters on the real embedded seekdb engine."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, event

from powercontext.builtin.artifacts.memory import EmbeddingProfile, Memory, MemoryEntryInput, MemorySearchRequest
from powercontext.builtin.artifacts.memory.service import MemoryService
from powercontext.builtin.inference import EmbeddingResult
from powercontext.builtin.persistence.artifacts import ArtifactRepository
from powercontext.builtin.persistence.memory import RelationalMemoryBackend
from powercontext.builtin.persistence.seekdb import SeekDBConfig
from powercontext.builtin.persistence.seekdb.memory_index import SeekDBMemoryIndex
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_contexts
from powercontext.builtin.tags import MemoryEntryTagTarget, TagFilter

PROFILE = EmbeddingProfile(
    profile_id="native-hybrid-filter-test", model="offline", dimension=3, distance="l2", normalization="unit"
)


class _Embedding:
    profile = PROFILE

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        return EmbeddingResult(vectors=tuple((1.0, 0.0, 0.0) for _ in texts))


@pytest.mark.asyncio
async def test_native_hybrid_filters_before_channel_limits_and_uses_both_indexes() -> None:
    # Short paths leave enough room for seekdb's Unix-domain socket name.
    path = Path(tempfile.mkdtemp(prefix="pc-hybrid-filter-")) / "db"
    async with open_builtin_contexts(
        BuiltinConfig(database=SeekDBConfig(path=path)), embedding_model=_Embedding()
    ) as contexts:
        memory = (await contexts.get("filter-scope")).artifacts.memory
        head = await memory.remember(
            memory=None,
            entries=tuple(MemoryEntryInput(kind="fact", text=f"Alpha record number {index}.") for index in range(40)),
            mode="append",
        )
        assert head is not None
        entries = await memory.entries(head)
        selected = entries[-1]
        for entry, tags in ((entries[-2], ("red",)), (selected, ("red", "blue"))):
            target = MemoryEntryTagTarget(artifact_id=head.artifact_id, entry_id=entry.entry_id)
            previous = await contexts.records.get_tags("filter-scope", target)
            await contexts.records.replace_tags("filter-scope", target, tags, expected_etag=previous.etag)
        extra_memory = MemoryService(
            backend=RelationalMemoryBackend(
                database=contexts.database,
                scope_id="filter-scope",
                artifacts=ArtifactRepository((Memory,)),
                index=SeekDBMemoryIndex(PROFILE),
            ),
            embedding_model=_Embedding(),
        )
        decoy = await extra_memory.remember(
            memory=None, entries=(MemoryEntryInput(kind="fact", text="Alpha decoy artifact."),), mode="append"
        )
        other = (await contexts.get("other-filter-scope")).artifacts.memory
        await other.remember(
            memory=None, entries=(MemoryEntryInput(kind="fact", text="Alpha decoy scope."),), mode="append"
        )
        assert decoy is not None
        index = SeekDBMemoryIndex(PROFILE)
        statements: list[tuple[str, Any]] = []

        def record(_connection, _cursor, statement, parameters, _context, _executemany):
            if "AGAINST" in statement.upper() or "l2_distance" in statement.lower():
                statements.append((statement, parameters))

        for tag_filter in (
            None,
            TagFilter(tags=("red", "blue"), match="all"),
            TagFilter(tags=("red", "blue"), match="any"),
            TagFilter(tags=("absent",)),
        ):
            request = MemorySearchRequest(
                query="alpha",
                analyzed_query="alpha",
                memories=(head.as_ref(),),
                mode="hybrid",
                candidate_limit=1,
                query_vector=(1.0, 0.0, 0.0),
                embedding_profile=PROFILE,
                tag_filter=tag_filter,
            )
            statements.clear()
            event.listen(Engine, "before_cursor_execute", record)
            try:
                async with contexts.database.transaction() as connection:
                    channels = await index.search(connection, "filter-scope", request)
            finally:
                event.remove(Engine, "before_cursor_execute", record)
            assert len(statements) == 1
            assert len(channels.fts) <= 1
            assert len(channels.vector) <= 1
            assert all(hit.memory_ref == head.as_ref() for hit in (*channels.fts, *channels.vector))
            if tag_filter is not None and "absent" in tag_filter.tags:
                assert not channels.fts and not channels.vector
            else:
                assert len(channels.fts) == len(channels.vector) == 1
            if tag_filter is not None and tag_filter.match == "all" and "red" in tag_filter.tags:
                assert channels.fts[0].entry_id == selected.entry_id
                assert channels.vector[0].entry_id == selected.entry_id
            if tag_filter is not None and tag_filter.match == "any":
                assert {hit.entry_id for hit in (*channels.fts, *channels.vector)} <= {
                    entries[-2].entry_id,
                    selected.entry_id,
                }
            statement, parameters = statements[0]
            async with contexts.database.transaction() as connection:
                plan = "\n".join(
                    str(row[0]) for row in (await connection.exec_driver_sql("EXPLAIN " + statement, parameters)).all()
                )
            print("HYBRID_FILTER", tag_filter, "CANDIDATE_STATEMENTS", len(statements))
            print(statement, parameters, plan, sep="\n")
            assert "TEXT RETRIEVAL SCAN" in plan
            assert "VECTOR INDEX" in plan

        empty_fts = await memory.search("unmatchedword", memories=(head,), mode="hybrid", limit=3)
        assert len(empty_fts.hits) == 3
        assert all(hit.matched_by == ("vector",) for hit in empty_fts.hits)
        both = await memory.search("alpha", memories=(head, decoy), mode="hybrid", limit=50)
        assert len(both.hits) == 41
        assert len({(hit.memory_ref.artifact_id, hit.entry_id, hit.entry_version_id) for hit in both.hits}) == 41
        assert {hit.memory_ref.artifact_id for hit in both.hits} == {head.artifact_id, decoy.artifact_id}

        inactive = await memory.forget(head, entries=(selected,))
        unavailable = await memory.search(
            "alpha",
            memories=(inactive,),
            mode="hybrid",
            limit=1,
            tag_filter=TagFilter(tags=("red", "blue"), match="all"),
        )
        assert not unavailable.hits
        restored = await memory.reactivate(inactive, entries=(selected,))
        recovered = await memory.search(
            "alpha",
            memories=(restored,),
            mode="hybrid",
            limit=1,
            tag_filter=TagFilter(tags=("red", "blue"), match="all"),
        )
        assert len(recovered.hits) == 1
        assert recovered.hits[0].entry_version_id == selected.entry_version_id
        assert recovered.hits[0].memory_ref == restored.as_ref()
        assert recovered.hits[0].matched_by == ("fts", "vector")
        empty_head = await memory.forget(restored, entries=entries)
        empty_result = await memory.search("alpha", memories=(empty_head,), mode="hybrid", limit=1)
        assert not empty_result.hits
