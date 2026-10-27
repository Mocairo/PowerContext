# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Single-statement native hybrid candidates for embedded seekdb."""

from __future__ import annotations

from pyobvector import VECTOR
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection
from typing_extensions import override

from powercontext.builtin.artifacts.memory import (
    CapabilityNotSupportedError,
    EmbeddingProfile,
    MemorySearchChannels,
    MemorySearchRequest,
)
from powercontext.builtin.artifacts.memory.canonical import validate_embedding
from powercontext.builtin.persistence.memory_index import CompositeMemoryIndex, memory_channel_hits
from powercontext.builtin.persistence.oceanbase.memory_index import OceanBaseMemoryFTSIndex, OceanBaseMemoryVectorIndex
from powercontext.builtin.persistence.tags import memory_tag_parameters, memory_tag_sql

_HYBRID_SQL = """
SELECT * FROM (
    SELECT 'fts' AS channel, f.memory_artifact_id, f.head_revision,
           f.entry_id, f.entry_version_id, v.text, NULL AS distance,
           -MATCH(f.searchable_text) AGAINST (:query) AS channel_order
    FROM pc_memory_entry_heads AS f
    JOIN pc_memory_entry_versions AS v
      ON v.scope_id = f.scope_id
     AND v.memory_artifact_id = f.memory_artifact_id
     AND v.entry_version_id = f.entry_version_id
    WHERE f.scope_id = :scope_id
      AND f.memory_artifact_id IN :memory_artifact_ids
      AND MATCH(f.searchable_text) AGAINST (:query)
      /* fts-tags */
    ORDER BY channel_order, f.memory_artifact_id, f.entry_id, f.entry_version_id
    LIMIT :candidate_limit
) AS fts_candidates
UNION ALL
SELECT * FROM (
    SELECT * FROM (
    SELECT 'vector' AS channel, m.memory_artifact_id, m.head_revision,
           m.entry_id, m.entry_version_id, v.text,
           l2_distance(m.embedding, :query_vector) AS distance,
           l2_distance(m.embedding, :query_vector) AS channel_order
    FROM pc_memory_vector_entries AS m
    JOIN pc_memory_entry_versions AS v
      ON v.scope_id = m.scope_id
     AND v.memory_artifact_id = m.memory_artifact_id
     AND v.entry_version_id = m.entry_version_id
    WHERE m.scope_id = :scope_id
      AND m.memory_artifact_id IN :memory_artifact_ids
      /* vector-tags */
    ORDER BY l2_distance(m.embedding, :query_vector) APPROXIMATE
    LIMIT :vector_scan_limit
    ) AS vector_scan
    ORDER BY distance, memory_artifact_id, entry_id, entry_version_id
    LIMIT :candidate_limit
) AS vector_candidates
ORDER BY channel, channel_order, memory_artifact_id, entry_id, entry_version_id
"""


class SeekDBMemoryIndex(CompositeMemoryIndex):
    """Retain the native projections and single channels, combining hybrid reads."""

    def __init__(self, profile: EmbeddingProfile) -> None:
        self._vector = OceanBaseMemoryVectorIndex(profile)
        super().__init__(OceanBaseMemoryFTSIndex(), self._vector)
        self._search_sql = text(_HYBRID_SQL).bindparams(
            bindparam("memory_artifact_ids", expanding=True),
            bindparam("query_vector", type_=VECTOR(profile.dimension)),
        )
        self._tagged_search_sql = text(
            _HYBRID_SQL.replace("/* fts-tags */", memory_tag_sql("f")).replace("/* vector-tags */", memory_tag_sql("m"))
        ).bindparams(
            bindparam("memory_artifact_ids", expanding=True),
            bindparam("tag_keys", expanding=True),
            bindparam("tag_hashes", expanding=True),
            bindparam("query_vector", type_=VECTOR(profile.dimension)),
        )

    @override
    async def search(
        self,
        connection: AsyncConnection,
        scope_id: str,
        request: MemorySearchRequest,
        /,
    ) -> MemorySearchChannels:
        if request.mode != "hybrid":
            return await super().search(connection, scope_id, request)
        if request.embedding_profile != self._vector.profile or request.query_vector is None:
            raise CapabilityNotSupportedError("embedding-profile")
        count = await self._vector.complete_count(connection, scope_id, request.memories, self._vector.profile)
        if count is None:
            raise CapabilityNotSupportedError("vector")
        rows = (
            (
                await connection.execute(
                    self._search_sql if request.tag_filter is None else self._tagged_search_sql,
                    {
                        "scope_id": scope_id,
                        "memory_artifact_ids": tuple(memory.artifact_id for memory in request.memories),
                        "query": request.analyzed_query,
                        "query_vector": validate_embedding(
                            request.query_vector, dimension=self._vector.profile.dimension
                        ),
                        "candidate_limit": request.candidate_limit,
                        # seekdb evaluates relational tag subqueries above the ANN
                        # operator. Cover the validated scope's vectors before that
                        # filter, then enforce top-k inside SQL. Otherwise selective
                        # tags can hide a match behind the ANN's early top-k limit.
                        "vector_scan_limit": request.candidate_limit if request.tag_filter is None else count,
                        **memory_tag_parameters(request.tag_filter),
                    },
                )
            )
            .mappings()
            .all()
        )
        # Current-head validation belongs to the backend. Unchanged projection
        # rows can retain older head_revision values after an append or tombstone.
        return MemorySearchChannels(
            fts=memory_channel_hits((row for row in rows if row["channel"] == "fts"), request.memories),
            vector=memory_channel_hits((row for row in rows if row["channel"] == "vector"), request.memories),
        )
