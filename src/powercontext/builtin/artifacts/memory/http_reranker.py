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

"""HTTP scoring with complete batch coverage and stable global selection."""

from __future__ import annotations

import math

import httpx
from pydantic import JsonValue

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    fields: dict[str, object] = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError("rerank response contains duplicate JSON fields")  # noqa: TRY003
        fields[key] = value
    return fields


def _batch_scores(payload: JsonValue, ranks: range) -> dict[int, int | float]:
    if not isinstance(payload, dict) or set(payload) != {"scores"} or not isinstance(payload["scores"], list):
        raise ValueError("rerank response must contain only a scores array")  # noqa: TRY003
    scores: dict[int, int | float] = {}
    for row in payload["scores"]:
        if not isinstance(row, dict) or set(row) != {"rank", "score"}:
            raise ValueError("rerank score records must contain exactly rank and score")  # noqa: TRY003
        rank, score = row["rank"], row["score"]
        if type(rank) is not int or rank not in ranks or rank in scores:
            raise ValueError("rerank response must cover each requested global rank exactly once")  # noqa: TRY003
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError("rerank score must be a finite number")  # noqa: TRY003, TRY004
        if isinstance(score, float) and not math.isfinite(score):
            raise ValueError("rerank score must be a finite number")  # noqa: TRY003
        scores[rank] = score
    if len(scores) != len(ranks):
        raise ValueError("rerank response is missing requested ranks")  # noqa: TRY003
    return scores


class HTTPMemoryReranker:
    """Score every candidate through a caller-owned client, then select globally."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = "http-cross-encoder-v1",
    ) -> None:
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("rerank batch_size must be a positive integer")  # noqa: TRY003
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        if not candidates or type(limit) is not int or not 1 <= limit <= len(candidates):
            raise ValueError("rerank requires candidates and an integer limit within their count")  # noqa: TRY003
        scores: dict[int, int | float] = {}
        requests = 0
        for start in range(0, len(candidates), self._batch_size):
            stop = min(start + self._batch_size, len(candidates))
            ranks = range(start + 1, stop + 1)
            response = await self._client.post(
                self._endpoint,
                json={
                    "query": query,
                    "candidates": [{"rank": rank, "text": candidates[rank - 1].text} for rank in ranks],
                },
                # Redirects are non-2xx failures, not extra unreported scoring calls.
                follow_redirects=False,
            )
            requests += 1
            response.raise_for_status()
            scores.update(_batch_scores(response.json(object_pairs_hook=_unique_fields), ranks))
        selected = sorted(scores, key=lambda rank: (-scores[rank], rank))[:limit]
        return MemoryRerankDecision(selected_ranks=tuple(selected), usage=InferenceUsage(requests=requests))


__all__ = ["HTTPMemoryReranker"]
