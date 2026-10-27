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

"""Share in-flight embedding batches and cache successful vectors in one event loop."""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel, QueryEmbeddingModel

_EmbeddingKey = tuple[bool, str]


@dataclass(frozen=True, slots=True)
class _EmbeddedText:
    vector: EmbeddingVector
    batch: object
    usage: InferenceUsage


def _observe_failure(future: asyncio.Future[_EmbeddedText]) -> None:
    # All waiters may have been cancelled before the provider fails.
    if not future.cancelled():
        future.exception()


class CoalescingEmbeddingModel:
    """Coalesce document embeddings without coupling callers' cancellation.

    Each caller reports the full provider usage of each batch it waits for, once
    per batch. Cached vectors carry no usage. Sharing is scoped to this instance
    and its event loop; the underlying model and profile are fixed at creation.
    """

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if type(max_batch_size) is not int or max_batch_size <= 0:
            raise ValueError("max_batch_size")
        if type(cache_size) is not int or cache_size < 0:
            raise ValueError("cache_size")
        if (
            isinstance(coalesce_ms, bool)
            or not isinstance(coalesce_ms, (float, int))
            or not math.isfinite(coalesce_ms)
            or coalesce_ms < 0
        ):
            raise ValueError("coalesce_ms")
        self.profile = model.profile
        self._model = model
        self._query_model = model if isinstance(model, QueryEmbeddingModel) else None
        self._max_batch_size = max_batch_size
        self._delay = coalesce_ms / 1000
        self._cache_size = cache_size
        self._cache: OrderedDict[_EmbeddingKey, EmbeddingVector] = OrderedDict()
        self._inflight: dict[_EmbeddingKey, asyncio.Future[_EmbeddedText]] = {}
        self._pending: dict[_EmbeddingKey, asyncio.Future[_EmbeddedText]] = {}
        self._flush: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Return vectors in input order, including repeated input positions."""
        return await self._embed(texts, query=False)

    async def embed_query(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Preserve asymmetric query embeddings without sharing document vectors."""
        return await self._embed(texts, query=self._query_model is not None)

    async def _embed(self, texts: tuple[str, ...], *, query: bool) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(vectors=())
        cached: dict[str, EmbeddingVector] = {}
        waiting: dict[str, asyncio.Future[_EmbeddedText]] = {}
        loop = asyncio.get_running_loop()
        # No await occurs while registering work, so another caller sees all of
        # this call's outstanding texts before the provider can start.
        for text in texts:
            key = (query, text)
            if key in self._cache:
                cached[text] = self._cache[key]
                self._cache.move_to_end(key)
            elif text not in waiting:
                future = self._inflight.get(key)
                if future is None:
                    future = loop.create_future()
                    future.add_done_callback(_observe_failure)
                    self._inflight[key] = self._pending[key] = future
                waiting[text] = future
        if self._pending and self._flush is None:
            self._flush = loop.call_later(self._delay, self._dispatch)
        completed = await asyncio.gather(*(asyncio.shield(future) for future in waiting.values()))
        usage_by_batch = {value.batch: value.usage for value in completed}
        vectors = dict(cached)
        vectors.update((text, value.vector) for text, value in zip(waiting, completed, strict=True))
        input_tokens = [usage.input_tokens for usage in usage_by_batch.values() if usage.input_tokens is not None]
        output_tokens = [usage.output_tokens for usage in usage_by_batch.values() if usage.output_tokens is not None]
        return EmbeddingResult(
            vectors=tuple(vectors[text] for text in texts),
            usage=InferenceUsage(
                requests=sum(usage.requests for usage in usage_by_batch.values()),
                input_tokens=sum(input_tokens) if input_tokens else None,
                output_tokens=sum(output_tokens) if output_tokens else None,
            ),
        )

    def _dispatch(self) -> None:
        pending = tuple(self._pending.items())
        self._pending.clear()
        self._flush = None
        # The same text can have different vectors for documents and queries.
        # Both namespaces share one bounded LRU, but never a provider batch.
        for query in (False, True):
            queue = tuple(item for item in pending if item[0][0] == query)
            embed = self._query_model.embed_query if query and self._query_model is not None else self._model.embed
            for offset in range(0, len(queue), self._max_batch_size):
                batch = queue[offset : offset + self._max_batch_size]
                task = asyncio.create_task(self._run_batch(batch, embed))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def _run_batch(
        self,
        batch: tuple[tuple[_EmbeddingKey, asyncio.Future[_EmbeddedText]], ...],
        embed: Callable[[tuple[str, ...]], Awaitable[EmbeddingResult]],
    ) -> None:
        try:
            result = await embed(tuple(key[1] for key, _ in batch))
            if len(result.vectors) != len(batch):
                raise ValueError("embedding-vector-count")  # noqa: TRY301 - fan out validation failures to all waiters
        except (Exception, asyncio.CancelledError) as error:
            for _, future in batch:
                future.set_exception(error)
        else:
            batch_id = object()
            for (key, future), vector in zip(batch, result.vectors, strict=True):
                if self._cache_size:
                    self._cache[key] = vector
                    self._cache.move_to_end(key)
                    while len(self._cache) > self._cache_size:
                        self._cache.popitem(last=False)
                future.set_result(_EmbeddedText(vector=vector, batch=batch_id, usage=result.usage))
        finally:
            for key, _ in batch:
                del self._inflight[key]


__all__ = ["CoalescingEmbeddingModel"]
