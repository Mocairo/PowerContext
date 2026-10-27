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

"""Durable lifecycle snapshots and current Topic eligibility."""

from __future__ import annotations

from sqlalchemy import delete, insert, select, update

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.persistence.codec import stored_bytes
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.sources import SourceRepository
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_TABLE, SOURCE_LIFECYCLE_TOPICS_TABLE
from powercontext.sources import Source, SourceRef


class SourceLifecycleStore:
    """Persist the small, single-writer dependency graph of one Scope."""

    def __init__(self, database: AsyncDatabase, sources: SourceRepository) -> None:
        self.database = database
        self.sources = sources

    async def source(self, scope_id: str, ref: SourceRef) -> Source:
        async with self.database.transaction() as connection:
            return (await self.sources.get(connection, scope_id, ref)).value

    async def load(self, scope_id: str) -> bytes | None:
        async with self.database.transaction() as connection:
            payload = await connection.scalar(
                select(SOURCE_LIFECYCLE_TABLE.c.payload).where(SOURCE_LIFECYCLE_TABLE.c.scope_id == scope_id)
            )
        return None if payload is None else stored_bytes(payload, column="payload")

    async def save(self, scope_id: str, payload: bytes, unavailable_topics: tuple[ArtifactRef, ...]) -> None:
        async with self.database.transaction() as connection:
            exists = await connection.scalar(
                select(SOURCE_LIFECYCLE_TABLE.c.scope_id).where(SOURCE_LIFECYCLE_TABLE.c.scope_id == scope_id)
            )
            if exists is None:
                await connection.execute(insert(SOURCE_LIFECYCLE_TABLE).values(scope_id=scope_id, payload=payload))
            else:
                await connection.execute(
                    update(SOURCE_LIFECYCLE_TABLE)
                    .where(SOURCE_LIFECYCLE_TABLE.c.scope_id == scope_id)
                    .values(payload=payload)
                )
            await connection.execute(
                delete(SOURCE_LIFECYCLE_TOPICS_TABLE).where(SOURCE_LIFECYCLE_TOPICS_TABLE.c.scope_id == scope_id)
            )
            for artifact_id, revision in sorted({(ref.artifact_id, ref.revision) for ref in unavailable_topics}):
                await connection.execute(
                    insert(SOURCE_LIFECYCLE_TOPICS_TABLE).values(
                        scope_id=scope_id, artifact_id=artifact_id, revision=revision
                    )
                )
