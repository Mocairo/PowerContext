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
        self.fail = False
        self.invalid = False

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        self.calls.append(texts)
        self.started.set()
        await self.release.wait()
        if self.fail and "bad" in texts:
            raise RuntimeError("unavailable")
        return EmbeddingResult(
            vectors=() if self.invalid else tuple((float(len(text)),) for text in texts),
            usage=InferenceUsage(requests=3, input_tokens=len(texts) * 10, output_tokens=2),
        )


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
                # A new observer synchronizes with the abandoned provider batch.
                with pytest.raises(RuntimeError, match="unavailable"):
                    await model.embed(("bad",))
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
