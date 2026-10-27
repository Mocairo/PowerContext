"""Strict HTTP scoring and global-rank behavior of the offline adapter."""

from __future__ import annotations

import asyncio
import json
import random

import httpx
import pytest

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory.http_reranker import HTTPMemoryReranker
from powercontext.builtin.artifacts.memory.models import MemoryHit


def _hit(rank: int) -> MemoryHit:
    return MemoryHit(
        memory_ref=ArtifactRef(family="memory", artifact_id=f"memory-{rank % 2}", revision=rank),
        entry_id=f"entry-{rank}",
        entry_version_id=f"version-{rank}",
        text=f"候选 {rank}\n完整正文",
        score=1 / rank,
        matched_by=("fts",),
    )


def test_batch_coverage_global_ties_client_ownership_and_numeric_precision():
    candidates = tuple(_hit(rank) for rank in range(1, 8))
    before = tuple(hit.model_dump() for hit in candidates)
    scores = {1: -0.0, 2: 2**64, 3: 2**64 + 1, 4: -7, 5: 2**64, 6: 0, 7: 2**64 + 2}
    requests = []

    def handle(request):
        assert request.method == "POST" and request.url.path == "/score"
        assert request.headers["x-client-header"] == "caller-owned"
        body = json.loads(request.content)
        assert set(body) == {"query", "candidates"}
        assert body["query"] == "中文查询"
        requests.append(body["candidates"])
        return httpx.Response(
            200,
            json={
                "scores": [{"rank": row["rank"], "score": scores[row["rank"]]} for row in reversed(body["candidates"])]
            },
        )

    async def scenario():
        async with httpx.AsyncClient(
            base_url="https://scoring.invalid",
            headers={"x-client-header": "caller-owned"},
            transport=httpx.MockTransport(handle),
        ) as client:
            reranker = HTTPMemoryReranker("/score", client=client, batch_size=3, policy_id="custom-policy")
            result = await reranker.rerank("中文查询", candidates, 6)
            assert result.selected_ranks == (7, 3, 2, 5, 1, 6)
            assert result.usage.requests == len(requests) == 3
            assert not result.used_fallback and result.discarded_rank_count == 0
            assert reranker.policy_id == "custom-policy" and not client.is_closed

    asyncio.run(scenario())
    assert [len(batch) for batch in requests] == [3, 3, 1]
    assert [row for batch in requests for row in batch] == [
        {"rank": rank, "text": hit.text} for rank, hit in enumerate(candidates, 1)
    ]
    assert tuple(hit.model_dump() for hit in candidates) == before


@pytest.mark.parametrize("value", [True, False, "2", 1.0, None, 0, -1])
def test_batch_size_rejects_nonpositive_and_noninteger_values(value):
    async def scenario():
        async with httpx.AsyncClient(trust_env=False) as client:
            with pytest.raises(ValueError):
                HTTPMemoryReranker("https://scoring.invalid", client=client, batch_size=value)

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [True, False, "1", 1.0, None, 0, -1, 2])
def test_invalid_limit_fails_before_any_http_request(value):
    def handle(_request):
        pytest.fail("invalid bounds must not send a request")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            reranker = HTTPMemoryReranker("https://scoring.invalid", client=client)
            with pytest.raises(ValueError):
                await reranker.rerank("query", (_hit(1),), value)
            with pytest.raises(ValueError):
                await reranker.rerank("query", (), 1)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "body",
    [
        b"not JSON",
        b"\xff",
        b"null",
        b"[]",
        b"{}",
        b'{"scores":null}',
        b'{"scores":{}}',
        b'{"scores":[]}',
        b'{"scores":[null]}',
        b'{"scores":[{"rank":1}]}',
        b'{"scores":[{"rank":1,"score":true}]}',
        b'{"scores":[{"rank":1,"score":"0.5"}]}',
        b'{"scores":[{"rank":1,"score":null}]}',
        b'{"scores":[{"rank":1,"score":[]}]}',
        b'{"scores":[{"rank":true,"score":0.5}]}',
        b'{"scores":[{"rank":1.0,"score":0.5}]}',
        b'{"scores":[{"rank":"1","score":0.5}]}',
        b'{"scores":[{"rank":0,"score":0.5}]}',
        b'{"scores":[{"rank":1,"score":-Infinity}]}',
        b'{"scores":[{"rank":1,"score":1e999}]}',
        b'{"scores":[{"rank":1,"score":0.5}],"extra":0}',
        b'{"scores":[],"scores":[{"rank":1,"score":0.5}]}',
        b'{"scores":[{"rank":1,"rank":1,"score":0.5}]}',
    ],
)
def test_malformed_or_ambiguous_service_payload_fails(body):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
        ) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://scoring.invalid", client=client).rerank("q", (_hit(1),), 1)

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [301, 307, 400, 429, 500, 503])
def test_http_errors_propagate_without_following_redirects(status):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, content=b"service failure", headers={"location": "/redirected"})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
            with pytest.raises(httpx.HTTPStatusError) as error:
                await HTTPMemoryReranker("https://scoring.invalid/score", client=client).rerank("q", (_hit(1),), 1)
            assert error.value.response.status_code == status
            assert len(requests) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("error_class", [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_transport_exception_is_not_wrapped(error_class):
    error = error_class("offline failure")

    def handle(_request):
        raise error

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(error_class) as caught:
                await HTTPMemoryReranker("https://scoring.invalid", client=client).rerank("q", (_hit(1),), 1)
            assert caught.value is error

    asyncio.run(scenario())


def test_invalid_later_batch_does_not_return_partial_or_fallback_ranking():
    requests = []

    def handle(request):
        rows = json.loads(request.content)["candidates"]
        requests.append(rows)
        return httpx.Response(200, json={"scores": [{"rank": 1, "score": 100}]})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://scoring.invalid", client=client, batch_size=1).rerank(
                    "q", (_hit(1), _hit(2), _hit(3)), 1
                )
        assert len(requests) == 2

    asyncio.run(scenario())


def test_concurrent_calls_have_independent_rank_and_request_accounting():
    async def handle(request):
        await asyncio.sleep(0)
        rows = json.loads(request.content)["candidates"]
        return httpx.Response(200, json={"scores": [{"rank": row["rank"], "score": row["rank"]} for row in rows]})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            reranker = HTTPMemoryReranker("https://scoring.invalid", client=client, batch_size=2)
            small, large = await asyncio.gather(
                reranker.rerank("small", (_hit(1),), 1),
                reranker.rerank("large", tuple(_hit(rank) for rank in range(1, 6)), 2),
            )
        assert small.selected_ranks == (1,) and small.usage.requests == 1
        assert large.selected_ranks == (5, 4) and large.usage.requests == 3

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("candidate_count", "batch_size", "limit"),
    [(1, 32, 1), (32, 32, 32), (33, 32, 1), (173, 7, 10), (101, 16, 9)],
)
def test_complete_candidate_pool_is_scored_across_batch_boundaries(candidate_count, batch_size, limit):
    rng = random.Random(2026)  # noqa: S311 - Reproducible score and response-order test data.
    scores = [rng.randrange(-20, 21) / 10 for _ in range(candidate_count)]
    requests = []

    def handle(request):
        rows = json.loads(request.content)["candidates"]
        assert 1 <= len(rows) <= batch_size
        requests.append(rows)
        result = [{"rank": row["rank"], "score": scores[row["rank"] - 1]} for row in rows]
        rng.shuffle(result)
        return httpx.Response(200, json={"scores": result})

    async def scenario():
        candidates = tuple(_hit(rank) for rank in range(1, candidate_count + 1))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await HTTPMemoryReranker("https://scoring.invalid", client=client, batch_size=batch_size).rerank(
                "batch coverage", candidates, limit
            )
        expected = sorted(enumerate(scores, 1), key=lambda pair: (-pair[1], pair[0]))[:limit]
        assert result.selected_ranks == tuple(rank for rank, _ in expected)
        assert result.usage.requests == len(requests) == (candidate_count + batch_size - 1) // batch_size
        assert [row for batch in requests for row in batch] == [
            {"rank": rank, "text": hit.text} for rank, hit in enumerate(candidates, 1)
        ]

    asyncio.run(scenario())
