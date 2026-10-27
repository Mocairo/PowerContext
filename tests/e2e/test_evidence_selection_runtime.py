"""Evidence selection shares native Memory state, citations, and incremental packs."""

from __future__ import annotations

import json

import pytest

from powercontext.artifacts import MemoryCitation
from powercontext.builtin.artifacts.memory import MemoryCandidateRequest, MemoryEntryInput
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CaptureSource,
    ContextAssembly,
    GetMemoryEntryRequest,
    PrepareContextRequest,
    RetireMemoryEntryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.context_pack import ContextPack, ContextPackDelta, apply_context_pack_delta
from powercontext.builtin.scope import ScopeDraft
from powercontext.builtin.sources import ContentSource


class _LiteralPipeline:
    async def extract(self, request: MemoryCandidateRequest, /):
        return tuple(
            MemoryEntryInput(kind="document", text=source.content, sources=(source,))
            for source in request.sources
            if isinstance(source, ContentSource)
        )


@pytest.mark.asyncio
async def test_prepare_selection_resolves_citations_and_respects_retirement_in_incremental_pack():
    text = "Administrative background. " * 80 + "Cobalt sensor pressure is 37 degrees."
    async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_LiteralPipeline()) as runtime:
        assert runtime.scopes is not None
        scope = await runtime.scopes.create(
            ScopeDraft(title="Evidence", summary="Literal evidence", idempotency_key="evidence")
        )
        memory = runtime.memory.for_scope(scope.scope_id)
        await runtime.sources.for_scope(scope.scope_id).capture(
            CaptureSource(source_id="document", content=text, metadata={})
        )
        while (await memory.flush()).processed:
            pass
        context = runtime.context.for_scope(scope.scope_id)
        request = PrepareContextRequest(
            query="cobalt sensor",
            evidence_selection=True,
            max_bytes=1200,
            assembly=ContextAssembly.model_validate({"sections": [{"family": "experience", "limit": 1}]}),
        )
        prepared = await context.prepare(request)
        assert prepared.content is not None
        items = json.loads(prepared.content.splitlines()[-2])["items"]
        assert any("37" in item["content"] for item in items)
        citations = []
        for item in items:
            citation = MemoryCitation.model_validate({
                key: value for key, value in item["citation"].items() if key != "scope_id"
            })
            citations.append(citation)
            record = await memory.get(GetMemoryEntryRequest(citation=citation))
            assert record.entry.text[item["start"] : item["end"]] == item["content"]
        pack = await context.prepare_incremental(request)
        assert isinstance(pack, ContextPack)
        assert b"".join(pack.items).decode() == prepared.content
        citation = citations[0]
        await memory.retire(RetireMemoryEntryRequest(citation=citation, reason="withdrawn"))
        delta = await context.prepare_incremental(request, pack)
        assert isinstance(delta, ContextPackDelta)
        assert apply_context_pack_delta(pack, delta).items == ()
        assert (await memory.get(GetMemoryEntryRequest(citation=citation))).entry.text == text
