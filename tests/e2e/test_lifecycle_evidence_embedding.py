"""Exercise lifecycle, cached embeddings, evidence packs and reranking together."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.artifacts.memory import EmbeddingProfile, MemoryEntryInput
from powercontext.builtin.artifacts.memory.http_reranker import HTTPMemoryReranker
from powercontext.builtin.inference import EmbeddingResult, InferenceUsage
from powercontext.builtin.inference.coalescing import CoalescingEmbeddingModel
from powercontext.builtin.persistence.seekdb import SeekDBConfig
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.records import ArtifactWrite
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CaptureSource,
    GetMemoryEntryRequest,
    PrepareContextRequest,
    RememberMemoryRequest,
    SearchMemoryRequest,
    SearchTopicMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.context_pack import ContextPack, ContextPackDelta, apply_context_pack_delta
from powercontext.builtin.runtime.source_lifecycle import SourceLifecycleError, SourceLifecycleEvent
from powercontext.builtin.scope import ScopeDraft


class _Embedding:
    profile = EmbeddingProfile(profile_id="integrated-cobalt-v1", model="offline", dimension=2)

    def __init__(self):
        self.calls = []

    async def embed(self, texts, /):
        self.calls.append(texts)
        return EmbeddingResult(
            vectors=tuple((1.0, 0.0) if "cobalt" in text else (0.0, 1.0) for text in texts),
            usage=InferenceUsage(requests=1, input_tokens=len(texts)),
        )


@pytest.mark.parametrize("backend", ["sqlite", "seekdb"])
def test_withdrawal_cannot_reenter_evidence_or_incremental_context_via_cached_vectors(tmp_path, backend):
    async def run():
        async with asyncio.timeout(180):
            database = (
                SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}")
                if backend == "sqlite"
                else SeekDBConfig(path=tmp_path / "seekdb")
            )
            config = BuiltinConfig(database=database)
            base = _Embedding()
            embeddings = CoalescingEmbeddingModel(base, max_batch_size=2, coalesce_ms=0, cache_size=32)
            rerank_inputs = []

            def score(request):
                body = json.loads(request.content)
                rerank_inputs.extend(candidate["text"] for candidate in body["candidates"])
                return httpx.Response(
                    200,
                    json={"scores": [{"rank": row["rank"], "score": 1 / row["rank"]} for row in body["candidates"]]},
                )

            async with httpx.AsyncClient(transport=httpx.MockTransport(score), trust_env=False) as client:
                reranker = HTTPMemoryReranker("http://offline.test/score", client=client, batch_size=2)
                async with open_builtin_runtime(
                    config, embedding_model=embeddings, memory_reranker=reranker
                ) as runtime:
                    assert runtime.scopes is not None
                    scope = await runtime.scopes.create(
                        ScopeDraft(title="Integration", summary="Four tasks", idempotency_key="integration")
                    )
                    scope_id = scope.scope_id
                    source = await runtime.sources.for_scope(scope_id).capture(
                        CaptureSource(
                            source_id="calibration", content="Original cobalt calibration source.", metadata={}
                        )
                    )
                    memory = runtime.memory.for_scope(scope_id)
                    body = "General administration. " * 70 + "The cobalt sensor is calibrated at 37 degrees. 原始😀"
                    written = await memory.remember(
                        RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text=body),))
                    )
                    assert written.entry is not None
                    original = written.entry.citation
                    await memory.remember(
                        RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text="Independent amber valve."),))
                    )
                    lifecycle = runtime.lifecycle.for_scope(scope_id)
                    await lifecycle.register_source("source", source.source_ref, revision=7)
                    await lifecycle.register_memory("memory", original, parents=("source",), policy="all")
                    topic = await runtime.records.for_scope(scope_id).create_artifact(
                        "topic-memory",
                        ArtifactWrite(
                            content={"title": "cobalt sensor", "summary": "cobalt sensor evidence", "detail": body}
                        ),
                    )
                    topic_ref = ArtifactRef(
                        family="topic-memory", artifact_id=topic.artifact_id, revision=topic.revision
                    )
                    await lifecycle.register_topic("topic", topic_ref, parents=("memory",), policy="all")
                    context = runtime.context.for_scope(scope_id)
                    request = PrepareContextRequest(query="cobalt sensor", evidence_selection=True, max_bytes=1400)
                    prepared = await context.prepare(request)
                    assert prepared.content and "37 degrees" in prepared.content
                    assert prepared.content_bytes == len(prepared.content.encode()) <= 1400
                    for item in json.loads(prepared.content.splitlines()[-2])["items"]:
                        citation = dict(item["citation"])
                        assert citation.pop("scope_id") == scope_id
                        record = await memory.get(
                            GetMemoryEntryRequest(citation=MemoryCitation.model_validate(citation))
                        )
                        assert item["content"] == record.entry.text[item["start"] : item["end"]]
                    pack = await context.prepare_incremental(request)
                    assert isinstance(pack, ContextPack)
                    assert b"".join(pack.items).decode() == prepared.content

                    withdrawn = SourceLifecycleEvent(
                        node_id="source", revision=7, sequence=1, available=False, reason="withdraw"
                    )
                    assert await lifecycle.apply(withdrawn) == []
                    rerank_inputs.clear()
                    for mode in ("fts", "vector", "hybrid"):
                        page = await memory.search(SearchMemoryRequest(query="cobalt sensor", mode=mode))
                        assert all(hit.entry_id != original.entry_id for hit in page.hits)
                    assert body not in rerank_inputs
                    for selection in (False, True):
                        inactive = await context.prepare(request.model_copy(update={"evidence_selection": selection}))
                        assert "37 degrees" not in (inactive.content or "")
                    assert not (
                        await runtime.topic_memory.for_scope(scope_id).search(
                            SearchTopicMemoryRequest(query="cobalt sensor")
                        )
                    ).hits
                    delta = await context.prepare_incremental(request, pack)
                    assert isinstance(delta, ContextPackDelta)
                    empty_pack = apply_context_pack_delta(pack, ContextPackDelta.from_bytes(delta.to_bytes()))
                    assert "37 degrees" not in b"".join(empty_pack.items).decode()
                    assert await lifecycle.resolve("memory", purpose="audit") == original
                    with pytest.raises(SourceLifecycleError):
                        await lifecycle.resolve("memory", purpose="generation")
                    assert (await memory.get(GetMemoryEntryRequest(citation=original))).entry.text == body

                # Persistent eligibility and references survive a real database reopen.
                async with open_builtin_runtime(
                    config, embedding_model=embeddings, memory_reranker=reranker
                ) as runtime:
                    lifecycle = runtime.lifecycle.for_scope(scope_id)
                    memory = runtime.memory.for_scope(scope_id)
                    with pytest.raises(SourceLifecycleError):
                        await lifecycle.resolve("topic", purpose="generation")
                    before = (await memory.list(include_inactive=True)).memory_ref
                    assert await lifecycle.apply(withdrawn) == []
                    assert (await memory.list(include_inactive=True)).memory_ref == before
                    restored = SourceLifecycleEvent(
                        node_id="source", revision=7, sequence=2, available=True, reason="restore"
                    )
                    assert await lifecycle.apply(restored) == ["memory", "source", "topic"]
                    assert await lifecycle.resolve("memory", purpose="generation") == original
                    for mode in ("fts", "vector", "hybrid"):
                        page = await memory.search(SearchMemoryRequest(query="cobalt sensor", mode=mode))
                        hit = next(hit for hit in page.hits if hit.entry_id == original.entry_id)
                        assert hit.entry_version_id == original.entry_version_id and hit.text == body
                        assert hit.memory_ref == page.memory_ref
                    assert (
                        await runtime.topic_memory.for_scope(scope_id).search(
                            SearchTopicMemoryRequest(query="cobalt sensor")
                        )
                    ).hits
                    context = runtime.context.for_scope(scope_id)
                    restored_pack = await context.prepare_incremental(request, empty_pack)
                    assert isinstance(restored_pack, ContextPackDelta)
                    final = apply_context_pack_delta(empty_pack, ContextPackDelta.from_bytes(restored_pack.to_bytes()))
                    assert b"".join(final.items).decode() == (await context.prepare(request)).content
                    assert "37 degrees" in b"".join(final.items).decode()
                    # Restoring native projections reuses the successful body vector.
                    assert sum(batch.count(body) for batch in base.calls) == 1

    asyncio.run(run())
