"""Incremental preparation preserves ordinary context and durable citations."""

from __future__ import annotations

import asyncio
import json

import pytest

from powercontext.builtin.artifacts.memory import MemoryCandidateRequest, MemoryCitation, MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CaptureSource,
    GetMemoryEntryRequest,
    PrepareContextRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.context_pack import (
    ContextPack,
    ContextPackDelta,
    ContextPackError,
    apply_context_pack_delta,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.builtin.sources import ContentSource


class _Pipeline:
    async def extract(self, request: MemoryCandidateRequest, /) -> tuple[MemoryEntryInput, ...]:
        return tuple(
            MemoryEntryInput(kind="conversation", text=source.content, sources=(source,))
            for source in request.sources
            if isinstance(source, ContentSource)
        )


def test_prepare_reconstructs_after_database_restart_and_preserves_each_citation(tmp_path):
    async def scenario():
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'context.db'}"))
        request = PrepareContextRequest(query="remote", max_bytes=8000)
        async with open_builtin_runtime(config, candidate_pipeline=_Pipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(ScopeDraft(title="Context", summary="Packs", idempotency_key="pack"))
            context = runtime.context.for_scope(scope.scope_id)
            empty = await context.prepare_incremental(request)
            assert isinstance(empty, ContextPack) and empty.items == ()
            assert (await context.prepare(request)).content is None
            for index, content in enumerate(("remote first 原始😀 evidence", "remote second original evidence")):
                await runtime.sources.for_scope(scope.scope_id).capture(
                    CaptureSource(source_id=f"turn-{index}", content=content, metadata={})
                )
            while (await runtime.memory.for_scope(scope.scope_id).flush()).processed:
                pass
            first = await context.prepare_incremental(request)
            assert isinstance(first, ContextPack)
            assert b"".join(first.items).decode() == (await context.prepare(request)).content
            first_wire = first.to_bytes()

        async with open_builtin_runtime(config, candidate_pipeline=_Pipeline()) as runtime:
            context = runtime.context.for_scope(scope.scope_id)
            await runtime.sources.for_scope(scope.scope_id).capture(
                CaptureSource(source_id="turn-later", content="remote later evidence", metadata={})
            )
            while (await runtime.memory.for_scope(scope.scope_id).flush()).processed:
                pass
            authorizations = []

            async def authorize(scopes):
                authorizations.append(scopes)

            previous = ContextPack.from_bytes(first_wire)
            delta = await context.prepare_incremental(request, previous, authorize_scopes=authorize)
            assert isinstance(delta, ContextPackDelta)
            assert authorizations == [(scope.scope_id,)]
            decoded = ContextPackDelta.from_bytes(delta.to_bytes())
            rebuilt = apply_context_pack_delta(previous, decoded)
            ordinary = await context.prepare(request)
            content = b"".join(rebuilt.items).decode()
            assert content == ordinary.content
            items = json.loads(content.splitlines()[-2])["items"]
            assert len(items) == 3
            for item in items:
                citation = MemoryCitation.model_validate(item["citation"])
                entry = await runtime.memory.for_scope(scope.scope_id).get(GetMemoryEntryRequest(citation=citation))
                assert entry.entry.text == item["content"]
                assert entry.citation == citation
                for reference in entry.entry.sources:
                    record = await runtime.records.for_scope(scope.scope_id).get_source(
                        reference.source_type, reference.source_id
                    )
                    assert record.source_type == reference.source_type and record.source_id == reference.source_id
                    assert record.content == item["content"]

            no_match = PrepareContextRequest(query="unrelatedzebramissingtoken")
            empty_delta = await context.prepare_incremental(no_match, rebuilt)
            assert isinstance(empty_delta, ContextPackDelta)
            assert apply_context_pack_delta(rebuilt, ContextPackDelta.from_bytes(empty_delta.to_bytes())).items == ()
            assert (await context.prepare(no_match)).content is None

    asyncio.run(scenario())


def test_invalid_base_is_rejected_and_authorization_failure_propagates():
    async def scenario():
        async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_Pipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(ScopeDraft(title="Context", summary="Pack", idempotency_key="pack"))
            context = runtime.context.for_scope(scope.scope_id)
            request = PrepareContextRequest(query="remote")
            with pytest.raises(ContextPackError):
                await context.prepare_incremental(request, base_pack=b"not a pack")  # ty: ignore[invalid-argument-type]

            async def deny(scopes):
                raise PermissionError("denied")

            with pytest.raises(PermissionError, match="denied"):
                await context.prepare_incremental(request, authorize_scopes=deny)

    asyncio.run(scenario())
