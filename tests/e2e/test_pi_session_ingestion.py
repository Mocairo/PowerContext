"""Pi recovery and exact evidence through the real SQLite Runtime boundaries."""

from __future__ import annotations

import asyncio
import copy
import json
import subprocess
import sys

import pytest

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.artifacts.memory.models import MemoryCitation
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CommitConnectorCheckpoint,
    GetMemoryEntryRequest,
    SearchMemoryRequest,
    SubmitSourceObservation,
    open_builtin_contexts,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.errors import ConnectorSubmissionRejectedError, InvalidSourceObservationError, SourceConflictError
from powercontext.limits import MAX_SOURCE_OBSERVATION_BYTES
from powercontext.sources import (
    ConnectorBinding,
    ConnectorRunStatus,
    SourceDefinitionRegistry,
    SourceMaterialization,
    SourceObservation,
    TextEvidence,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.connectors import ConnectorLifecycle
from powercontext.sources.pi_session import (
    PI_EVENT_SOURCE_DEFINITION,
    PiEventInput,
    PiSessionConnector,
    PiSessionInput,
    parse_pi_session,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


def _message(event_id, parent_id, text, role="assistant"):
    return {"type": "message", "id": event_id, "parentId": parent_id, "message": {"role": role, "content": text}}


def _write(path, records, session_id="pi-recovery"):
    header = {"type": "session", "version": 3, "id": session_id}
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in [header, *records]) + "\n", encoding="utf-8"
    )


def _config(database):
    return BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{database}"))


async def _prepare(runtime):
    scope = await runtime.scopes.create(
        ScopeDraft(title="Pi recovery", summary="Offline evidence", idempotency_key="pi-recovery")
    )
    await runtime.ingestion.register(manifest_for_definition(PI_EVENT_SOURCE_DEFINITION))
    return ConnectorBinding(
        scope_id=scope.scope_id, binding_id="pi", connector_name="pi-session-history", connector_version="1"
    )


class _RuntimeSink:
    def __init__(self, runtime, fault=None):
        self.runtime = runtime
        self.registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
        self.fault = fault

    async def submit(self, binding, item_id, definition_name, value):
        assert definition_name == "pi-event"
        if value.event_id == "b" and self.fault == "before-submit":
            raise OSError("injected submission failure")  # noqa: TRY003
        if value.event_id == "b" and self.fault == "rejection":
            raise ConnectorSubmissionRejectedError("injected rejection")  # noqa: TRY003
        source = await self.registry.resolve(value)
        receipt = await self.runtime.ingestion.submit(
            SubmitSourceObservation(
                scope_id=binding.scope_id, observation=project_source_for_transport(self.registry, source)
            )
        )
        if value.event_id == "a" and self.fault == "after-submit":
            raise OSError("injected failure after durable receipt")  # noqa: TRY003
        return receipt.source_ref


class _RuntimeCheckpoints:
    def __init__(self, runtime, fault=None):
        self.runtime, self.fault = runtime, fault

    async def load(self, binding):
        return (await self.runtime.ingestion.checkpoint(binding)).checkpoint

    async def save(self, binding, checkpoint, *, expected):
        if self.fault == "checkpoint":
            raise OSError("injected checkpoint failure")  # noqa: TRY003
        await self.runtime.ingestion.commit(
            CommitConnectorCheckpoint(binding=binding, checkpoint=checkpoint, expected=expected)
        )


async def _run(path, database, leaf=None, fault=None):
    async with open_builtin_runtime(_config(database)) as runtime:
        binding = await _prepare(runtime)
        return await ConnectorLifecycle(
            sink=_RuntimeSink(runtime, fault), checkpoints=_RuntimeCheckpoints(runtime, fault)
        ).run(PiSessionConnector(path, leaf), binding)


async def _state(database):
    async with open_builtin_runtime(_config(database)) as runtime:
        binding = await _prepare(runtime)
        checkpoint = (await runtime.ingestion.checkpoint(binding)).checkpoint
    async with open_builtin_contexts(_config(database)) as contexts:
        entries = await (await contexts.get(binding.scope_id)).sources.journal.entries()
    return binding, checkpoint, entries


async def _events(entries):
    registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
    return tuple([
        await registry.read(PI_EVENT_SOURCE_DEFINITION.source_class.model_validate(row.source.payload))
        for row in entries
    ])


_PROCESS_RUN = """
import asyncio
import json
import runpy
import sys
from pathlib import Path

helpers = runpy.run_path(sys.argv[1])
result = asyncio.run(helpers['_run'](Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4] or None))
Path(sys.argv[5]).write_text(result.model_dump_json(), encoding='utf-8')
"""


def _run_process(path, database, leaf):
    result_file = path.with_suffix(".result.json")
    subprocess.run(
        [sys.executable, "-c", _PROCESS_RUN, __file__, str(path), str(database), leaf or "", str(result_file)],
        check=True,
        timeout=90,
        capture_output=True,
        text=True,
    )
    return json.loads(result_file.read_text(encoding="utf-8"))


def test_actual_process_restart_branch_switch_append_and_replay(tmp_path):
    path, database = tmp_path / "session.jsonl", tmp_path / "history.db"
    records = [
        _message("root", None, "shared evidence", "user"),
        _message("left", "root", "left evidence"),
        {"type": "model_change", "id": "control", "parentId": "root", "modelId": "offline"},
        _message("right", "control", "right evidence"),
    ]
    _write(path, records)
    first = _run_process(path, database, "left")
    second = _run_process(path, database, "right")
    assert len(first["items"]) == 2 and len(second["items"]) == 1
    assert second["previous_checkpoint"] == first["committed_checkpoint"]
    replay = _run_process(path, database, "right")
    assert replay["items"] == []
    assert replay["committed_checkpoint"] == second["committed_checkpoint"]
    records.append(_message("tail", "right", "appended evidence"))
    _write(path, records)
    appended = _run_process(path, database, None)
    assert len(appended["items"]) == 1
    assert appended["previous_checkpoint"] == second["committed_checkpoint"]
    old_branch = _run_process(path, database, "left")
    assert old_branch["items"] == []

    async def verify():
        _, checkpoint, entries = await _state(database)
        assert checkpoint == old_branch["committed_checkpoint"]
        assert [event.event_id for event in await _events(entries)] == ["root", "left", "right", "tail"]
        assert [event.record for event in await _events(entries)] == [records[index] for index in (0, 1, 3, 4)]
        assert [entry.position for entry in entries] == [1, 2, 3, 4]

    asyncio.run(verify())


@pytest.mark.parametrize("fault", ["before-submit", "after-submit", "checkpoint", "rejection"])
def test_durable_sources_survive_failed_run_and_retry_without_duplicate_journal(tmp_path, fault):
    async def scenario():
        path, database = tmp_path / "session.jsonl", tmp_path / "history.db"
        records = [_message("root", None, "original root", "user")]
        _write(path, records)
        original = await _run(path, database)
        records.extend([_message("a", "root", "first addition"), _message("b", "a", "second addition")])
        _write(path, records)
        if fault == "rejection":
            failed = await _run(path, database, fault=fault)
            assert failed.status is ConnectorRunStatus.INCOMPLETE
            assert failed.committed_checkpoint == original.committed_checkpoint
        else:
            with pytest.raises(OSError, match="injected"):
                await _run(path, database, fault=fault)
        _, checkpoint, partial = await _state(database)
        assert checkpoint == original.committed_checkpoint
        partial_ids = [event.event_id for event in await _events(partial)]
        assert partial_ids == (["root", "a", "b"] if fault == "checkpoint" else ["root", "a"])
        restored = await _run(path, database)
        assert restored.previous_checkpoint == original.committed_checkpoint
        assert restored.status is ConnectorRunStatus.COMPLETE
        _, checkpoint, complete = await _state(database)
        assert checkpoint == restored.committed_checkpoint
        assert [event.event_id for event in await _events(complete)] == ["root", "a", "b"]
        assert [entry.position for entry in complete] == [1, 2, 3]
        assert complete[: len(partial)] == partial
        assert (await _run(path, database)).items == ()

    asyncio.run(scenario())


def test_changed_partially_persisted_event_cannot_overwrite_before_checkpoint_recovery(tmp_path):
    async def scenario():
        path, database = tmp_path / "session.jsonl", tmp_path / "history.db"
        records = [_message("root", None, "original root"), _message("a", "root", "original addition")]
        _write(path, records[:1])
        initial = await _run(path, database)
        _write(path, records)
        with pytest.raises(OSError):
            await _run(path, database, fault="after-submit")
        _, _, original_entries = await _state(database)
        records[1]["message"]["content"] = "changed uncheckpointed addition"
        _write(path, records)
        with pytest.raises(ValueError):
            await _run(path, database)
        _, checkpoint, entries = await _state(database)
        assert checkpoint == initial.committed_checkpoint and entries == original_entries
        records[1]["message"]["content"] = "original addition"
        _write(path, records)
        await _run(path, database)
        assert (await _state(database))[2] == original_entries

    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["json", "encoding", "missing-parent", "duplicate-id", "missing-leaf"])
def test_whole_archive_validation_precedes_new_ingestion_and_repair_resumes(tmp_path, damage):
    async def scenario():
        path, database = tmp_path / "session.jsonl", tmp_path / "history.db"
        records = [_message("root", None, "root")]
        _write(path, records)
        initial = await _run(path, database)
        original_entries = (await _state(database))[2]
        records.append(_message("a", "root", "valid pending evidence"))
        _write(path, records)
        suffixes = {
            "json": b"{invalid json}\n",
            "encoding": b"\xff\n",
            "missing-parent": b'{"type":"model_change","id":"bad","parentId":"absent"}\n',
            "duplicate-id": b'{"type":"model_change","id":"root","parentId":null}\n',
            "missing-leaf": b"",
        }
        path.write_bytes(path.read_bytes() + suffixes[damage])
        with pytest.raises(ValueError):
            await _run(path, database, "absent" if damage == "missing-leaf" else "a")
        _, checkpoint, entries = await _state(database)
        assert checkpoint == initial.committed_checkpoint and entries == original_entries
        _write(path, records)
        repaired = await _run(path, database)
        assert repaired.previous_checkpoint == initial.committed_checkpoint
        assert len(repaired.items) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["text", "raw-metadata", "parent", "delete"])
def test_imported_other_branch_mutation_is_detected_before_new_branch_writes(tmp_path, mutation):
    async def scenario():
        path, database = tmp_path / "session.jsonl", tmp_path / "history.db"
        records = [_message("root", None, "shared"), _message("left", "root", "left")]
        _write(path, records)
        initial = await _run(path, database, "left")
        initial_entries = (await _state(database))[2]
        original = copy.deepcopy(records)
        records.append(_message("right", "root", "new right"))
        if mutation == "text":
            records[1]["message"]["content"] = "changed left"
        elif mutation == "raw-metadata":
            records[1]["providerMetadata"] = {"notRendered": True}
        elif mutation == "parent":
            records[1]["parentId"] = None
        else:
            del records[1]
        _write(path, records)
        with pytest.raises(ValueError):
            await _run(path, database, "right")
        _, checkpoint, entries = await _state(database)
        assert checkpoint == initial.committed_checkpoint and entries == initial_entries
        _write(path, [*original, _message("right", "root", "new right")])
        repaired = await _run(path, database, "right")
        assert len(repaired.items) == 1

    asyncio.run(scenario())


def test_source_identity_components_and_immutable_raw_record(tmp_path):
    async def scenario():
        registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
        pairs = [("a:b", "c"), ("a", "b:c"), ("a/b", "c"), ("a", "b/c"), ("é", "e\u0301"), ("e\u0301", "é")]
        pairs += [("会话" * 1000, "节点" * 1000), ("x", " event "), ("x", "event")]
        sources = []
        for session_id, event_id in pairs:
            path = tmp_path / "identity.jsonl"
            record = _message(event_id, None, "payload")
            record["providerMetadata"] = {"integer": 2**63 - 1, "negativeZero": -0.0, "float": 1.0}
            _write(path, [record], session_id=session_id)
            event = parse_pi_session(PiSessionInput(content=path.read_bytes()))[0]
            source = await registry.resolve(event)
            assert len(source.name) <= 256
            assert source.materialization is SourceMaterialization.CAPTURED
            assert source.name == (await registry.resolve(event.model_copy(deep=True))).name
            readback = await registry.read(source)
            assert isinstance(readback, PiEventInput)
            assert readback == event
            assert json.dumps(readback.record["providerMetadata"], sort_keys=True) == json.dumps(
                record["providerMetadata"], sort_keys=True
            )
            sources.append(source)
        assert len({source.name for source in sources}) == len(pairs)
        database = tmp_path / "immutable.db"
        original_event = await registry.read(sources[0])
        assert isinstance(original_event, PiEventInput)
        changed_record = copy.deepcopy(original_event.record)
        changed_record["hiddenMetadata"] = {"changed": True}
        changed_event = original_event.model_copy(update={"record": changed_record})
        changed = await registry.resolve(changed_event)
        assert changed.name == sources[0].name
        async with open_builtin_runtime(_config(database)) as runtime:
            binding = await _prepare(runtime)
            original_observation = project_source_for_transport(registry, sources[0])
            await runtime.ingestion.submit(
                SubmitSourceObservation(scope_id=binding.scope_id, observation=original_observation)
            )
            with pytest.raises(SourceConflictError):
                await runtime.ingestion.submit(
                    SubmitSourceObservation(
                        scope_id=binding.scope_id, observation=project_source_for_transport(registry, changed)
                    )
                )
        entries = (await _state(database))[2]
        assert len(entries) == 1 and entries[0].source == original_observation
        persisted_event = (await _events(entries))[0]
        assert json.dumps(persisted_event.record["providerMetadata"], sort_keys=True) == json.dumps(
            original_event.record["providerMetadata"], sort_keys=True
        )

    asyncio.run(scenario())


def test_rewriting_control_ancestor_cannot_change_imported_branch_history(tmp_path):
    async def scenario():
        path, database = tmp_path / "session.jsonl", tmp_path / "history.db"
        records = [
            _message("root", None, "original root"),
            _message("other", None, "other root"),
            {"type": "model_change", "id": "control", "parentId": "root"},
            _message("leaf", "control", "captured leaf"),
        ]
        _write(path, records)
        initial = await _run(path, database)
        initial_entries = (await _state(database))[2]
        records[2]["parentId"] = "other"
        records.append(_message("tail", "leaf", "pending evidence"))
        _write(path, records)
        with pytest.raises(ValueError):
            await _run(path, database)
        _, checkpoint, entries = await _state(database)
        assert checkpoint == initial.committed_checkpoint and entries == initial_entries
        records[2]["parentId"] = "root"
        _write(path, records)
        repaired = await _run(path, database)
        assert len(repaired.items) == 1

    asyncio.run(scenario())


class _BoundedMemoryPipeline:
    async def extract(self, request, /):
        # Memory is a bounded search aid; the captured Source remains complete.
        return tuple(
            MemoryEntryInput(
                kind="pi-event",
                text=source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"][:512],
                sources=(source,),
            )
            for source in request.sources
        )


def test_existing_runtime_envelope_limit_rejects_without_truncation_or_progress(tmp_path):
    async def scenario():
        path, database = tmp_path / "session.jsonl", tmp_path / "history.db"
        records = [_message("root", None, "original root")]
        _write(path, records)
        initial = await _run(path, database)
        original_entries = (await _state(database))[2]
        large_result = "oversized payload " + "x" * (MAX_SOURCE_OBSERVATION_BYTES // 2)
        records.append({
            "type": "message",
            "id": "large",
            "parentId": "root",
            "message": {"role": "toolResult", "toolName": "read", "toolCallId": "call", "content": large_result},
        })
        _write(path, records)
        event = parse_pi_session(PiSessionInput(content=path.read_bytes()))[-1]
        registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
        source = await registry.resolve(event)
        observation = project_source_for_transport(registry, source)
        evidence = TextEvidence.model_validate(observation.projection(TEXT_EVIDENCE_PROJECTION_KEY))
        assert evidence.content.endswith(large_result)
        assert len(observation.model_dump_json().encode("utf-8")) > MAX_SOURCE_OBSERVATION_BYTES
        with pytest.raises(InvalidSourceObservationError, match="4 MiB"):
            await _run(path, database)
        _, checkpoint, entries = await _state(database)
        assert checkpoint == initial.committed_checkpoint and entries == original_entries
        result_message = records[-1]["message"]
        assert isinstance(result_message, dict)
        result_message["content"] = "corrected tool result"
        _write(path, records)
        repaired = await _run(path, database)
        assert repaired.previous_checkpoint == initial.committed_checkpoint and len(repaired.items) == 1
        assert (await _events((await _state(database))[2]))[-1].record == records[-1]

    asyncio.run(scenario())


def test_each_retrieved_citation_reads_exact_full_source_including_large_tool_result(tmp_path):
    async def scenario():
        path, database = tmp_path / "session.jsonl", tmp_path / "history.db"
        tokens = ["pialpharoot", "pibetacall", "pigammaresult", "pideltacompact", "piepsilonbranch", "pizetacustom"]
        large_result = tokens[2] + "\n" + ("完整工具结果 😀\r\n" * 20000) + "TAIL original evidence"
        records = [
            _message("root", None, tokens[0], "user"),
            {
                **_message("call", "root", []),
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "toolCall", "name": "read", "id": "call-1", "arguments": {"query": tokens[1]}}
                    ],
                },
            },
            {
                **_message("result", "call", []),
                "message": {"role": "toolResult", "toolName": "read", "toolCallId": "call-1", "content": large_result},
            },
            {"type": "compaction", "id": "summary", "parentId": "result", "summary": tokens[3]},
            {"type": "branch_summary", "id": "branch", "parentId": "summary", "summary": tokens[4]},
            {"type": "custom_message", "id": "custom", "parentId": "branch", "content": tokens[5]},
        ]
        _write(path, records)
        expected = parse_pi_session(PiSessionInput(content=path.read_bytes()))
        result = await _run(path, database)
        assert len(result.items) == len(records)
        binding, _, entries = await _state(database)
        assert await _events(entries) == expected
        stored = {entry.source_ref.source_id: entry.source for entry in entries}
        assert len(expected[2].content.encode("utf-8")) > 8192
        async with open_builtin_runtime(_config(database), candidate_pipeline=_BoundedMemoryPipeline()) as runtime:
            memory = runtime.memory.for_scope(binding.scope_id)
            for _ in range(len(records) + 1):
                if not (await memory.flush()).processed:
                    break
            else:
                pytest.fail("Memory processing did not drain the captured sources")
        # Reopen both search and source readers after processing to verify durable references.
        async with (
            open_builtin_runtime(_config(database)) as runtime,
            open_builtin_contexts(_config(database)) as contexts,
        ):
            memory = runtime.memory.for_scope(binding.scope_id)
            catalog = (await contexts.get(binding.scope_id)).sources.catalog
            registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
            for token, event, source_entry in zip(tokens, expected, entries, strict=True):
                page = await memory.search(SearchMemoryRequest(query=token, mode="fts", limit=20))
                matched = []
                for hit in page.hits:
                    citation = MemoryCitation(
                        memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id
                    )
                    retrieved = await memory.get(GetMemoryEntryRequest(citation=citation))
                    for ref in retrieved.entry.sources:
                        if ref == source_entry.source_ref:
                            persisted = await catalog.get(stored[ref.source_id])
                            assert isinstance(persisted, SourceObservation)
                            native = PI_EVENT_SOURCE_DEFINITION.source_class.model_validate(persisted.payload)
                            assert await registry.read(native) == event
                            evidence = TextEvidence.model_validate(persisted.projection(TEXT_EVIDENCE_PROJECTION_KEY))
                            assert evidence.source_id == ref.source_id == persisted.name
                            assert evidence.source_type == "pi-event"
                            assert evidence.content == event.content
                            assert evidence.metadata == {
                                "session_id": event.session_id,
                                "event_id": event.event_id,
                                "parent_id": event.parent_id,
                                "event_type": event.event_type,
                                "role": event.role,
                            }
                            matched.append(ref)
                assert matched == [source_entry.source_ref]
        assert (await _state(database))[2] == entries

    asyncio.run(scenario())
