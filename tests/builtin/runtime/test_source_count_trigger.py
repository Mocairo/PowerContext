"""Count admission uses durable identities independently of scheduling and retries."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import delete, update

from powercontext.builtin.persistence.cursors import SourceCursorRepository
from powercontext.builtin.persistence.processing_intents import ArtifactProcessingIntentRepository
from powercontext.builtin.persistence.sources import SourceRepository
from powercontext.builtin.persistence.tables import SOURCES_TABLE
from powercontext.builtin.runtime.artifact_processing import ArtifactProcessingBinding, ArtifactProcessingSupervisor
from powercontext.builtin.runtime.config import BuiltinConfig, RuntimeConfig
from powercontext.builtin.runtime.processing_discovery import source_count_ready
from powercontext.builtin.runtime.processing_registry import canonical_processing_manifest
from powercontext.builtin.sources import SourceCursor
from powercontext.builtin.triggers import SOURCE_WINDOW_TRIGGER_NAME
from powercontext.sources import SourceMaterialization
from tests.builtin.persistence.contract import SOURCE_ADAPTERS, NoteSource
from tests.builtin.runtime.test_processing_scheduler import (
    _CycleSupervisor,
    _intent,
    _Launcher,
    _profile,
    _request,
    _wait,
)


async def _seed(database, scope, count):
    async with database.transaction() as connection:
        repository = SourceRepository(SOURCE_ADAPTERS)
        for index in range(count):
            source = NoteSource(name=str(index), materialization=SourceMaterialization.CAPTURED, body=f"source {index}")
            await repository.add(connection, scope, source)
            # Identical replay must not affect the admission count.
            await repository.add(connection, scope, source)
    await _request(database, "memory-binding", scope, requested=False)


def test_count_uses_rows_after_each_family_cursor_not_journal_high_watermark(tmp_path):
    async def scenario():
        async with _profile(tmp_path, "counts") as profile:
            await _seed(profile.database, "a", 4)
            await _seed(profile.database, "b", 1)
            async with profile.database.transaction() as connection:
                # A sparse imported journal still counts identities, not offsets.
                await connection.execute(
                    update(SOURCES_TABLE)
                    .where(SOURCES_TABLE.c.scope_id == "a", SOURCES_TABLE.c.journal_position == 4)
                    .values(journal_position=40)
                )
                await connection.execute(
                    delete(SOURCES_TABLE).where(SOURCES_TABLE.c.scope_id == "a", SOURCES_TABLE.c.journal_position == 3)
                )
                await SourceCursorRepository().save(
                    connection, "a", "memory-binding", SourceCursor(sequence=2), expected_generation=None
                )
                assert await source_count_ready(connection, "memory-binding", ("a", "b"), 2) == frozenset()
                assert await source_count_ready(connection, "memory-binding", ("a", "b"), 1) == {"a", "b"}
                assert await source_count_ready(connection, "topic-binding", ("a", "b"), 3) == {"a"}
                assert await source_count_ready(connection, "memory-binding", (), 1) == frozenset()
                threshold = 2**80
                assert RuntimeConfig(memory_source_count_threshold=threshold).memory_source_count_threshold == threshold
                assert await source_count_ready(connection, "memory-binding", ("a", "b"), threshold) == frozenset()

    asyncio.run(scenario())


def test_below_threshold_scopes_do_not_hide_a_ready_scope_on_later_pages(tmp_path):
    async def scenario():
        async with _profile(tmp_path, "pages") as profile:
            for index in range(105):
                await _seed(profile.database, f"scope-{index:03}", 1)
            await _seed(profile.database, "ready", 3)
            launcher = _Launcher(profile.database)
            async with ArtifactProcessingSupervisor(
                database=profile.database,
                bindings=(ArtifactProcessingBinding("memory-binding", "memory", launcher, source_count_threshold=3),),
                lease_mode="single-process",
            ) as supervisor:
                await _wait(lambda: supervisor.family_status["memory"]["completed"] == 1)
                assert [work.scope_id for work in launcher.assignments] == ["ready"]
                assert (await _intent(profile.database, "memory-binding", "scope-000")).requested_generation == 0
                assert (await _intent(profile.database, "memory-binding", "scope-104")).requested_generation == 0

    asyncio.run(scenario())


class _FailAdmissionOnce(ArtifactProcessingIntentRepository):
    failed = False

    async def request(self, connection, scope_id, binding_name, /):
        result = await super().request(connection, scope_id, binding_name)
        if not self.failed:
            self.failed = True
            raise OSError("injected count admission rollback")  # noqa: TRY003
        return result


def test_count_admission_rollback_is_retried_without_skipping_inputs(tmp_path):
    async def scenario():
        async with _profile(tmp_path, "admission-rollback") as profile:
            for scope in ("a", "b", "c"):
                await _seed(profile.database, scope, 2)
            launcher = _Launcher(profile.database)
            async with ArtifactProcessingSupervisor(
                database=profile.database,
                bindings=(ArtifactProcessingBinding("memory-binding", "memory", launcher, source_count_threshold=2),),
                lease_mode="single-process",
                intents=_FailAdmissionOnce(),
                oceanbase_tick_seconds=0.02,
            ) as supervisor:
                await _wait(lambda: supervisor.family_status["memory"]["completed"] == 3)
                assert [work.scope_id for work in launcher.assignments] == ["a", "b", "c"]
                for scope in ("a", "b", "c"):
                    intent = await _intent(profile.database, "memory-binding", scope)
                    assert intent.requested_generation == intent.handled_generation == 1

    asyncio.run(scenario())


def test_count_admission_failure_waits_for_backoff_without_spinning(tmp_path):
    async def scenario():
        async with _profile(tmp_path, "admission-backoff") as profile:
            await _seed(profile.database, "scope", 2)
            launcher = _Launcher(profile.database)
            async with _CycleSupervisor(
                database=profile.database,
                bindings=(ArtifactProcessingBinding("memory-binding", "memory", launcher, source_count_threshold=2),),
                lease_mode="single-process",
                intents=_FailAdmissionOnce(),
                oceanbase_tick_seconds=0.5,
            ) as supervisor:
                assert supervisor.status.value == "degraded"
                cycles = supervisor.cycles
                await asyncio.sleep(0.1)
                # A deferred scan must not consume CPU in a millisecond loop.
                assert supervisor.cycles - cycles < 10
                await _wait(lambda: supervisor.family_status["memory"]["completed"] == 1)
                assert len(launcher.assignments) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("threshold", [None, 100])
def test_accepted_failure_recovers_after_restart_even_when_new_threshold_is_unmet(tmp_path, threshold):
    async def scenario():
        async with _profile(tmp_path, "accepted") as profile:
            await _seed(profile.database, "scope", 2)

            async def fail(_work):
                raise OSError("injected worker failure")  # noqa: TRY003

            launcher = _Launcher(profile.database, on_work=fail)
            async with ArtifactProcessingSupervisor(
                database=profile.database,
                bindings=(ArtifactProcessingBinding("memory-binding", "memory", launcher, source_count_threshold=2),),
                lease_mode="single-process",
            ) as supervisor:
                await _wait(lambda: int(supervisor.family_status["memory"]["failed"]) >= 1)
                saved = await _intent(profile.database, "memory-binding", "scope")
                assert saved.requested_generation == 1 and saved.handled_generation == 0
        # Restart the storage connection pool too, as closing/reopening Runtime does.
        async with _profile(tmp_path, "accepted") as profile:
            recovered = _Launcher(profile.database)
            async with ArtifactProcessingSupervisor(
                database=profile.database,
                bindings=(
                    ArtifactProcessingBinding("memory-binding", "memory", recovered, source_count_threshold=threshold),
                ),
                lease_mode="single-process",
            ) as supervisor:
                await _wait(lambda: supervisor.family_status["memory"]["completed"] == 1)
                assert len(recovered.assignments) == 1
                assert recovered.assignments[0].claimed_request_generation == saved.requested_generation
                saved = await _intent(profile.database, "memory-binding", "scope")
                assert saved.requested_generation == saved.handled_generation == 1

    asyncio.run(scenario())


def test_count_only_memory_is_included_in_automatic_recovery_manifest():
    config = BuiltinConfig(runtime=RuntimeConfig(memory_source_count_threshold=3))
    assert SOURCE_WINDOW_TRIGGER_NAME in canonical_processing_manifest(config)["legacy_automatic_bindings"]
    assert SOURCE_WINDOW_TRIGGER_NAME not in canonical_processing_manifest(BuiltinConfig())["legacy_automatic_bindings"]
