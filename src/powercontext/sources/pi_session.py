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

"""Captured Pi branch evidence and durable, restartable history collection."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from powercontext.errors import SourceConflictError
from powercontext.sources._pi_session_parser import PiEventInput, PiSessionInput, _parse_archive, parse_pi_session
from powercontext.sources.connectors import (
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
    ConnectorSubmissionStatus,
)
from powercontext.sources.definitions import SourceDefinition, SourceProjection
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _identity(session_id: str, event_id: str) -> str:
    # An encoded pair avoids delimiter ambiguity, even for long or Unicode IDs.
    digest = hashlib.sha256(_json([session_id, event_id]).encode("utf-8")).hexdigest()
    return f"pi-event:{digest}"


class PiEventSource(Source):
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    # RFC 8785 is used by the Source store. A JSON string preserves integers
    # beyond its numeric range and float representations in the original record.
    record_json: str


class _PiEventTextProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type="pi-event",
            source_id=source.name,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


class _PiEventSourceDefinition:
    name = "pi-event"
    version = "1"
    input_class = PiEventInput
    source_class = PiEventSource
    projections: tuple[SourceProjection[PiEventSource], ...] = (_PiEventTextProjection(),)

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        if not value.session_id or not value.event_id:
            raise ValueError("Pi session and event IDs must be nonempty")  # noqa: TRY003
        return PiEventSource(
            name=_identity(value.session_id, value.event_id),
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record_json=_json(value.record),
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=json.loads(source.record_json),
        )


PI_EVENT_SOURCE_DEFINITION: SourceDefinition[PiEventInput, PiEventSource, PiEventInput] = _PiEventSourceDefinition()


class _PiCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    session_id: str
    imported: tuple[str, ...] = ()
    records: dict[str, str] = Field(default_factory=dict)


def _checkpoint_for_archive(
    session_id: str,
    records: dict[str, dict[str, JsonValue]],
    leaf_id: str | None,
    previous: JsonValue | None,
) -> _PiCheckpoint:
    checkpoint = _PiCheckpoint(session_id=session_id) if previous is None else _PiCheckpoint.model_validate(previous)
    if checkpoint.session_id != session_id:
        raise ValueError("Pi archive session ID changed for this binding")  # noqa: TRY003

    # Verify all captured branches before the first write, not only the
    # selected leaf. Canonical JSON makes formatting/key-order edits harmless.
    fingerprints: dict[str, str] = {}
    for event_id, expected in checkpoint.records.items():
        record = records.get(event_id)
        if record is None:
            raise ValueError(f"Previously captured Pi node {event_id!r} is missing")  # noqa: TRY003
        actual = hashlib.sha256(_json(record).encode("utf-8")).hexdigest()
        if actual != expected:
            raise ValueError(f"Previously captured Pi node {event_id!r} was modified")  # noqa: TRY003
        fingerprints[event_id] = actual

    if not set(checkpoint.imported).issubset(checkpoint.records):
        raise ValueError("Pi checkpoint is missing imported record checksums")  # noqa: TRY003

    # Remember control nodes too: rewiring one would rewrite the ancestry
    # of an otherwise unchanged captured event.
    node_id = leaf_id if leaf_id is not None else next(reversed(records), None)
    while node_id is not None and node_id not in fingerprints:
        record = records[node_id]
        fingerprints[node_id] = hashlib.sha256(_json(record).encode("utf-8")).hexdigest()
        parent_id = record["parentId"]
        node_id = parent_id if isinstance(parent_id, str) else None
    return checkpoint.model_copy(update={"records": fingerprints})


class PiSessionConnector:
    """Collect one archive through ConnectorLifecycle and its durable Source sink.

    A binding belongs to one session. Branch selection may change on every run;
    previously captured branches remain immutable, including their ancestry.
    """

    name = "pi-session-history"
    version = "1"
    source_definitions = frozenset({"pi-event"})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self.path = Path(path)
        self.leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        content = await asyncio.to_thread(self.path.read_bytes)
        session_id, records, events = _parse_archive(PiSessionInput(content=content, leaf_id=self.leaf_id))
        checkpoint = _checkpoint_for_archive(session_id, records, self.leaf_id, session.checkpoint)
        imported = set(checkpoint.imported)
        newly_imported: list[str] = []
        for event in events:
            if event.event_id in imported:
                continue
            try:
                outcome = await session.submit(_identity(session_id, event.event_id), "pi-event", event)
            except SourceConflictError as error:
                # A preceding run may have persisted a Source before failing to
                # commit its checkpoint. Runtime protects that evidence as well.
                raise ValueError(f"Persisted Pi event {event.event_id!r} was modified") from error  # noqa: TRY003
            if outcome.status is not ConnectorSubmissionStatus.ACCEPTED:
                return ConnectorRunCompletion(status=ConnectorRunStatus.INCOMPLETE, checkpoint=session.checkpoint)
            newly_imported.append(event.event_id)

        proposed = _PiCheckpoint(
            session_id=session_id,
            imported=(*checkpoint.imported, *newly_imported),
            records=checkpoint.records,
        )
        return ConnectorRunCompletion(status=ConnectorRunStatus.COMPLETE, checkpoint=proposed.model_dump(mode="json"))


__all__ = [
    "PI_EVENT_SOURCE_DEFINITION",
    "PiEventInput",
    "PiEventSource",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
]
