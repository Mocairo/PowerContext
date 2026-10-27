"""Observable concurrency, failure isolation, and usage contracts for embeddings."""

from __future__ import annotations

import asyncio
import gc

import pytest

from powercontext.builtin.artifacts.memory import EmbeddingProfile
from powercontext.builtin.inference import EmbeddingResult, InferenceUsage
from powercontext.builtin.inference.coalescing import CoalescingEmbeddingModel


class ControlledEmbedding:
    profile = EmbeddingProfile(profile_id="coalescing-tests", model="offline", dimension=1)

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()
        self.fail = False
        self.invalid = False

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        self.calls.append(texts)
        self.started.set()
        try:
            await self.release.wait()
            if self.fail and "bad" in texts:
                raise RuntimeError("unavailable")
            return EmbeddingResult(
                vectors=() if self.invalid else tuple((float(len(text)),) for text in texts),
                usage=InferenceUsage(requests=3, input_tokens=len(texts) * 10, output_tokens=2),
            )
        finally:
            self.finished.set()


def test_late_waiter_shares_running_work_with_cache_disabled() -> None:
    async def run() -> None:
        async with asyncio.timeout(3):
            base = ControlledEmbedding()
            model = CoalescingEmbeddingModel(base, coalesce_ms=0, cache_size=0)
            first = asyncio.create_task(model.embed(("a", "bbbb", "a")))
            await base.started.wait()
            late = asyncio.create_task(model.embed(("bbbb", "a", "bbbb")))
            # The late coroutine registers before this event-loop callback releases work.
            asyncio.get_running_loop().call_soon(base.release.set)
            left, right = await asyncio.gather(first, late)
            assert left.vectors == ((1.0,), (4.0,), (1.0,))
            assert right.vectors == ((4.0,), (1.0,), (4.0,))
            assert left.usage == right.usage == InferenceUsage(requests=3, input_tokens=20, output_tokens=2)
            assert base.calls == [("a", "bbbb")]
            await model.embed(("a",))
            assert base.calls == [("a", "bbbb"), ("a",)]
            assert model.profile is base.profile

    asyncio.run(run())


def test_each_waiter_accounts_only_for_its_distinct_batches() -> None:
    async def run() -> None:
        async with asyncio.timeout(3):
            base = ControlledEmbedding()
            base.release.set()
            model = CoalescingEmbeddingModel(base, max_batch_size=2, coalesce_ms=0)
            left, right = await asyncio.gather(model.embed(("a", "b", "c", "a")), model.embed(("c", "c")))
            assert base.calls == [("a", "b"), ("c",)]
            assert left.usage == InferenceUsage(requests=6, input_tokens=30, output_tokens=4)
            assert right.usage == InferenceUsage(requests=3, input_tokens=10, output_tokens=2)
            mixed = await model.embed(("c", "dddd", "c"))
            assert mixed.vectors == ((1.0,), (4.0,), (1.0,))
            assert mixed.usage == InferenceUsage(requests=3, input_tokens=10, output_tokens=2)
            assert (await model.embed(("a", "b", "c", "dddd"))).usage.requests == 0

    asyncio.run(run())


def test_failed_batch_does_not_block_independent_batch_or_retry() -> None:
    async def run() -> None:
        async with asyncio.timeout(3):
            base = ControlledEmbedding()
            base.fail = True
            base.release.set()
            model = CoalescingEmbeddingModel(base, max_batch_size=1, coalesce_ms=0)
            failed, shared, independent = await asyncio.gather(
                model.embed(("bad", "good")), model.embed(("bad",)), model.embed(("good",)), return_exceptions=True
            )
            assert isinstance(failed, RuntimeError)
            assert isinstance(shared, RuntimeError)
            assert isinstance(independent, EmbeddingResult)
            assert independent.vectors == ((4.0,),)
            base.fail = False
            result = await model.embed(("bad", "good"))
            assert result.vectors == ((3.0,), (4.0,))
            assert result.usage.requests == 3
            assert base.calls == [("bad",), ("good",), ("bad",)]

    asyncio.run(run())


def test_cancelled_waiters_leave_no_unhandled_provider_exception() -> None:
    async def run() -> None:
        async with asyncio.timeout(3):
            loop = asyncio.get_running_loop()
            errors = []
            previous = loop.get_exception_handler()
            loop.set_exception_handler(lambda _loop, context: errors.append(context))
            try:
                base = ControlledEmbedding()
                base.fail = True
                model = CoalescingEmbeddingModel(base, coalesce_ms=0, cache_size=0)
                waiters = [asyncio.create_task(model.embed(("bad",))) for _ in range(2)]
                await base.started.wait()
                for waiter in waiters:
                    waiter.cancel()
                assert all(
                    isinstance(value, asyncio.CancelledError)
                    for value in await asyncio.gather(*waiters, return_exceptions=True)
                )
                base.release.set()
                # Synchronize on provider completion without attaching another waiter.
                await base.finished.wait()
                await asyncio.sleep(0)
                gc.collect()
                assert errors == []
                base.fail = False
                assert (await model.embed(("bad",))).vectors == ((3.0,),)
                del waiters, model
                gc.collect()
                await asyncio.sleep(0)
                assert errors == []
            finally:
                loop.set_exception_handler(previous)

    asyncio.run(run())


def test_cancelled_waiter_during_window_does_not_remove_survivors_work() -> None:
    async def run() -> None:
        async with asyncio.timeout(3):
            base = ControlledEmbedding()
            base.release.set()
            model = CoalescingEmbeddingModel(base, coalesce_ms=20)
            cancelled = asyncio.create_task(model.embed(("same",)))
            survivor = asyncio.create_task(model.embed(("same",)))
            asyncio.get_running_loop().call_soon(cancelled.cancel)
            result = await asyncio.gather(cancelled, survivor, return_exceptions=True)
            assert isinstance(result[0], asyncio.CancelledError)
            assert isinstance(result[1], EmbeddingResult)
            assert result[1].vectors == ((4.0,),)
            assert base.calls == [("same",)]

    asyncio.run(run())


def test_invalid_batch_caches_no_vectors_and_can_be_retried() -> None:
    async def run() -> None:
        async with asyncio.timeout(3):
            base = ControlledEmbedding()
            base.release.set()
            base.invalid = True
            model = CoalescingEmbeddingModel(base, coalesce_ms=0)
            with pytest.raises(ValueError):
                await model.embed(("a", "bb"))
            base.invalid = False
            assert (await model.embed(("a", "bb"))).vectors == ((1.0,), (2.0,))
            assert base.calls == [("a", "bb"), ("a", "bb")]

    asyncio.run(run())


@pytest.mark.parametrize(
    "options",
    [
        {"max_batch_size": 0},
        {"max_batch_size": True},
        {"max_batch_size": 1.5},
        {"cache_size": -1},
        {"cache_size": False},
        {"coalesce_ms": -1},
        {"coalesce_ms": float("inf")},
        {"coalesce_ms": float("nan")},
    ],
)
def test_invalid_configuration_fails_before_scheduling(options) -> None:
    with pytest.raises(ValueError):
        CoalescingEmbeddingModel(ControlledEmbedding(), **options)


def test_provider_cancellation_is_local_to_its_batch_and_can_be_retried():
    class Provider(ControlledEmbedding):
        async def embed(self, texts, /):
            if self.fail and "cancel" in texts:
                raise asyncio.CancelledError
            return await super().embed(texts)

    async def run():
        async with asyncio.timeout(3):
            base = Provider()
            base.release.set()
            base.fail = True
            model = CoalescingEmbeddingModel(base, max_batch_size=1, coalesce_ms=0)
            failed, survived = await asyncio.gather(
                model.embed(("cancel",)), model.embed(("ok",)), return_exceptions=True
            )
            assert isinstance(failed, asyncio.CancelledError)
            assert isinstance(survived, EmbeddingResult) and survived.vectors == ((2.0,),)
            base.fail = False
            assert (await model.embed(("cancel",))).vectors == ((6.0,),)

    asyncio.run(run())


def test_asymmetric_queries_have_separate_vectors_but_share_the_cache_bound():
    class Asymmetric(ControlledEmbedding):
        def __init__(self):
            super().__init__()
            self.query_calls = []

        async def embed_query(self, texts, /):
            self.query_calls.append(texts)
            return EmbeddingResult(
                vectors=tuple((-float(len(text)),) for text in texts), usage=InferenceUsage(requests=2)
            )

    async def run():
        async with asyncio.timeout(3):
            base = Asymmetric()
            base.release.set()
            model = CoalescingEmbeddingModel(base, coalesce_ms=0, cache_size=1)
            assert (await model.embed(("same",))).vectors == ((4.0,),)
            first, second = await asyncio.gather(model.embed_query(("same", "same")), model.embed_query(("same",)))
            assert first.vectors == ((-4.0,), (-4.0,))
            assert second.vectors == ((-4.0,),)
            assert first.usage.requests == second.usage.requests == 2
            assert base.query_calls == [("same",)]
            assert (await model.embed_query(("same",))).usage.requests == 0
            # Query and document vectors together use one bounded cache.
            assert (await model.embed(("same",))).usage.requests == 3
            assert base.calls == [("same",), ("same",)]

    asyncio.run(run())


def test_asymmetric_wrapper_preserves_real_memory_query_semantics():
    from powercontext.builtin.artifacts.memory import MemoryEntryInput
    from powercontext.builtin.persistence.sqlite import SQLiteConfig
    from powercontext.builtin.runtime import BuiltinConfig, open_builtin_contexts

    class Asymmetric:
        profile = EmbeddingProfile(profile_id="asymmetric-tests", model="offline", dimension=2)

        async def embed(self, texts, /):
            return EmbeddingResult(
                vectors=tuple((0.0, 1.0) if text == "Beta document" else (1.0, 0.0) for text in texts)
            )

        async def embed_query(self, texts, /):
            return EmbeddingResult(vectors=tuple((0.0, 1.0) for _ in texts))

    async def run():
        async with asyncio.timeout(10):
            model = CoalescingEmbeddingModel(Asymmetric(), coalesce_ms=0)
            async with open_builtin_contexts(BuiltinConfig(database=SQLiteConfig()), embedding_model=model) as contexts:
                memory = (await contexts.get("asymmetric")).artifacts.memory
                head = await memory.remember(
                    memory=None,
                    entries=tuple(
                        MemoryEntryInput(kind="fact", text=text) for text in ("Alpha document", "Beta document")
                    ),
                    mode="append",
                )
                assert head is not None
                result = await memory.search("find beta", memories=(head,), mode="vector", limit=1)
                assert result.hits[0].text == "Beta document"

    asyncio.run(run())
