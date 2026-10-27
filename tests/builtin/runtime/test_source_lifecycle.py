"""Lifecycle boundaries and durable recovery through real native Memory writes."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryEntryInput, MemoryService
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.records import ArtifactWrite
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CaptureSource,
    GetMemoryEntryRequest,
    RememberMemoryRequest,
    SearchMemoryRequest,
    SearchTopicMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.source_lifecycle import SourceLifecycleError, SourceLifecycleEvent
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import SourceRef


def _event(*, sequence=1, available=False, reason="withdrawal"):
    return SourceLifecycleEvent(node_id="source", revision=3, sequence=sequence, available=available, reason=reason)


async def _fixture(runtime, name="test"):
    scope = (await runtime.scopes.create(ScopeDraft(title=name, summary=name, idempotency_key=name))).scope_id
    lifecycle = runtime.lifecycle.for_scope(scope)
    source = (
        await runtime.sources.for_scope(scope).capture(
            CaptureSource(source_id="source", content="original", metadata={})
        )
    ).source_ref
    await lifecycle.register_source("source", source, revision=3)
    memory = runtime.memory.for_scope(scope)
    created = await memory.remember(
        RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text="stable needle"),))
    )
    citation = created.entry.citation
    await lifecycle.register_memory("memory", citation, parents=("source",), policy="all")
    artifact = await runtime.records.for_scope(scope).create_artifact(
        "topic-memory",
        ArtifactWrite(content={"title": "needle", "summary": "needle summary", "detail": "needle detail"}),
    )
    topic = ArtifactRef(family="topic-memory", artifact_id=artifact.artifact_id, revision=artifact.revision)
    await lifecycle.register_topic("topic", topic, parents=("memory",), policy="any")
    return scope, lifecycle, source, citation, topic


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("node_id", ""),
        ("node_id", " \t"),
        ("node_id", 1),
        ("reason", "\n"),
        ("reason", False),
        ("revision", True),
        ("revision", 1.0),
        ("revision", "1"),
        ("revision", -1),
        ("sequence", False),
        ("sequence", 0),
        ("sequence", 2.0),
        ("sequence", "2"),
        ("available", 1),
        ("available", "false"),
        ("extra", "not permitted"),
    ],
)
def test_event_fields_are_strict(field, value):
    values = {"node_id": "source", "revision": 3, "sequence": 1, "available": False, "reason": "withdrawal"}
    values[field] = value
    with pytest.raises(ValidationError):
        SourceLifecycleEvent(**values)


def test_registrations_validate_real_references_dependencies_and_binding_identity():
    async def run():
        async with open_builtin_runtime(BuiltinConfig(database=SQLiteConfig())) as runtime:
            scope, lifecycle, source, citation, topic = await _fixture(runtime)
            before = (await runtime.memory.for_scope(scope).list()).memory_ref
            await lifecycle.register_source("source", source, revision=3)
            await lifecycle.register_memory("memory", citation, parents=("source",), policy="all")
            await lifecycle.register_topic("topic", topic, parents=("memory",), policy="any")
            assert (await runtime.memory.for_scope(scope).list()).memory_ref == before
            for parents in ((), ("missing",), ("source", "source"), ("new",)):
                with pytest.raises(SourceLifecycleError):
                    await lifecycle.register_memory("new", citation, parents=parents, policy="all")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("memory", citation, parents=("topic",), policy="all")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("new", citation, parents=("source",), policy="none")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_source("source", source, revision=4)
            for revision in (True, 0, -1, 1.0, "3"):
                with pytest.raises(SourceLifecycleError):
                    await lifecycle.register_source("new", source, revision=revision)
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_source("new", SourceRef(source_type="content", source_id="absent"), revision=3)
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory(
                    "new", citation.model_copy(update={"entry_version_id": "absent"}), parents=("source",), policy="all"
                )
            for reference in (topic.model_copy(update={"artifact_id": "absent"}), citation.memory_ref):
                with pytest.raises(SourceLifecycleError):
                    await lifecycle.register_topic("new", reference, parents=("source",), policy="all")
            for node, purpose in (("absent", "audit"), ("memory", "unknown")):
                with pytest.raises(SourceLifecycleError):
                    await lifecycle.resolve(node, purpose=purpose)
            with pytest.raises(SourceLifecycleError):
                await lifecycle.apply(_event().model_copy(update={"node_id": "memory"}))
            assert (await runtime.memory.for_scope(scope).list()).memory_ref == before

    asyncio.run(run())


@pytest.mark.parametrize("failure_after_commit", [False, True])
def test_interrupted_event_retries_after_reopen_without_duplicate_memory_revisions(
    tmp_path, monkeypatch, failure_after_commit
):
    async def run():
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'retry.db'}"))
        original = MemoryService.forget

        async def interrupted(self, *args, **kwargs):
            if failure_after_commit:
                await original(self, *args, **kwargs)
            raise OSError("simulated-interruption")

        async with open_builtin_runtime(config) as runtime:
            scope, lifecycle, _, citation, topic = await _fixture(runtime)
            initial = (await runtime.memory.for_scope(scope).list()).memory_ref
            assert initial is not None
            with monkeypatch.context() as patch:
                patch.setattr(MemoryService, "forget", interrupted)
                with pytest.raises(OSError):
                    await lifecycle.apply(_event())
            intermediate = (await runtime.memory.for_scope(scope).list(include_inactive=True)).memory_ref
            assert intermediate is not None
            assert intermediate.revision == initial.revision + int(failure_after_commit)
        async with open_builtin_runtime(config) as runtime:
            lifecycle = runtime.lifecycle.for_scope(scope)
            # The accepted sequence survives a crash even if native writes fail.
            with pytest.raises(SourceLifecycleError):
                await lifecycle.apply(_event(reason="conflicting replay"))
            assert await lifecycle.apply(_event()) == []
            memory = runtime.memory.for_scope(scope)
            completed = await memory.list(include_inactive=True)
            assert completed.memory_ref is not None
            assert completed.memory_ref.revision == initial.revision + 1
            assert completed.entries[0].state == "inactive"
            assert not (await memory.search(SearchMemoryRequest(query="needle", mode="fts"))).hits
            assert not (
                await runtime.topic_memory.for_scope(scope).search(SearchTopicMemoryRequest(query="needle"))
            ).hits
            assert not (await runtime.topic_memory.for_scope(scope).browse(limit=10))
            assert (await memory.get(GetMemoryEntryRequest(citation=citation))).entry.text == "stable needle"
            assert await lifecycle.resolve("topic", purpose="audit") == topic
            for node in ("source", "memory", "topic"):
                with pytest.raises(SourceLifecycleError):
                    await lifecycle.resolve(node, purpose="generation")
            await lifecycle.apply(_event())
            assert (await memory.list(include_inactive=True)).memory_ref == completed.memory_ref
            assert await lifecycle.apply(_event(sequence=2, available=True)) == ["memory", "source", "topic"]
            assert await lifecycle.resolve("memory", purpose="generation") == citation
            restored = await memory.list()
            assert restored.entries[0].entry.entry_version_id == citation.entry_version_id
            assert (await runtime.topic_memory.for_scope(scope).search(SearchTopicMemoryRequest(query="needle"))).hits
            assert len(await runtime.topic_memory.for_scope(scope).browse(limit=10)) == 1

    asyncio.run(run())


def test_scope_isolation_and_unbounded_positive_event_integers_and_reason(tmp_path):
    async def run():
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'scopes.db'}"))
        async with open_builtin_runtime(config) as runtime:
            first, lifecycle, source, citation, topic = await _fixture(runtime, "first")
            second, other, _, other_citation, _ = await _fixture(runtime, "second")
            with pytest.raises(SourceLifecycleError):
                await other.register_memory("foreign", citation, parents=("source",), policy="all")
            with pytest.raises(SourceLifecycleError):
                await other.register_topic("foreign", topic, parents=("source",), policy="all")
            huge = 10**100
            assert await lifecycle.apply(_event(sequence=huge, reason="撤回😀" * 1000)) == []
            await lifecycle.register_source("source", source, revision=3)
            assert not (await runtime.memory.for_scope(first).list()).entries
            assert len((await runtime.memory.for_scope(second).list()).entries) == 1
            assert await other.resolve("memory", purpose="generation") == other_citation
        async with open_builtin_runtime(config) as runtime:
            lifecycle = runtime.lifecycle.for_scope(first)
            assert await lifecycle.apply(_event(sequence=huge, reason="撤回😀" * 1000)) == []
            with pytest.raises(SourceLifecycleError):
                await lifecycle.apply(_event(sequence=huge - 1, available=True))
            await lifecycle.apply(_event(sequence=huge + 1, available=True))
            assert (await runtime.memory.for_scope(first).list()).entries
            assert (await runtime.memory.for_scope(second).list()).entries

    asyncio.run(run())


def test_interrupted_late_registration_and_restore_are_retryable(tmp_path, monkeypatch):
    async def run():
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'late.db'}"))

        async def interrupted(*args, **kwargs):
            raise OSError("simulated-interruption")

        async with open_builtin_runtime(config) as runtime:
            scope, lifecycle, _, _, _ = await _fixture(runtime)
            await lifecycle.apply(_event())
            memory = runtime.memory.for_scope(scope)
            late = await memory.remember(
                RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text="later needle"),))
            )
            assert late.entry is not None
            citation = late.entry.citation
            with monkeypatch.context() as patch:
                patch.setattr(MemoryService, "forget", interrupted)
                with pytest.raises(OSError):
                    await lifecycle.register_memory("late", citation, parents=("source",), policy="any")
            await lifecycle.register_memory("late", citation, parents=("source",), policy="any")
            assert not (await memory.list()).entries
            with monkeypatch.context() as patch:
                patch.setattr(MemoryService, "reactivate", interrupted)
                with pytest.raises(OSError):
                    await lifecycle.apply(_event(sequence=2, available=True))
        async with open_builtin_runtime(config) as runtime:
            lifecycle = runtime.lifecycle.for_scope(scope)
            assert await lifecycle.apply(_event(sequence=2, available=True)) == ["late", "memory", "source", "topic"]
            memory = runtime.memory.for_scope(scope)
            assert len((await memory.list()).entries) == 2
            assert await lifecycle.resolve("late", purpose="generation") == citation
            before = (await memory.list()).memory_ref
            await lifecycle.register_memory("late", citation, parents=("source",), policy="any")
            await lifecycle.apply(_event(sequence=2, available=True))
            assert (await memory.list()).memory_ref == before

    asyncio.run(run())


def test_native_aliases_require_equivalent_dependencies():
    async def run():
        async with open_builtin_runtime(BuiltinConfig(database=SQLiteConfig())) as runtime:
            scope, lifecycle, source, citation, topic = await _fixture(runtime)
            await lifecycle.register_source("independent", source, revision=3)
            await lifecycle.register_memory("memory-alias", citation, parents=("source",), policy="all")
            await lifecycle.register_topic("topic-alias", topic, parents=("memory",), policy="any")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("conflict", citation, parents=("independent",), policy="all")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_topic("conflict", topic, parents=("independent",), policy="any")
            assert await lifecycle.apply(_event()) == ["independent"]
            assert not (await runtime.memory.for_scope(scope).list()).entries
            for node in ("memory", "memory-alias", "topic", "topic-alias"):
                with pytest.raises(SourceLifecycleError):
                    await lifecycle.resolve(node, purpose="generation")
            assert await lifecycle.resolve("memory-alias", purpose="audit") == citation
            assert await lifecycle.resolve("topic-alias", purpose="audit") == topic
            await lifecycle.apply(_event(sequence=2, available=True))
            assert await lifecycle.resolve("memory-alias", purpose="generation") == citation
            assert await lifecycle.resolve("topic-alias", purpose="generation") == topic
            assert len((await runtime.memory.for_scope(scope).list()).entries) == 1

    asyncio.run(run())
