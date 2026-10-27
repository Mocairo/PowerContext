"""Real local HTTP scoring through SQLite Memory search and exact readback."""

from __future__ import annotations

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.artifacts.memory.http_reranker import HTTPMemoryReranker
from powercontext.builtin.artifacts.memory.models import MemoryCitation
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    RememberMemoryRequest,
    RuntimeConfig,
    SearchMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft


def test_local_http_search_preserves_exact_entries_and_reopens_without_mutation(tmp_path):
    scores = {
        "project alpha evidence": 0.4,
        "project beta evidence": 0.9,
        "project gamma evidence": 0.9,
        "project delta evidence": 0.2,
        "project epsilon evidence": 0.99,
        "project zeta evidence": -1,
    }
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/score"
            assert self.headers["x-client-header"] == "native-runtime"
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            requests.append(body)
            result = {
                "scores": [{"rank": row["rank"], "score": scores[row["text"]]} for row in reversed(body["candidates"])]
            }
            encoded = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format, *args):  # noqa: A002 - Match the HTTP handler's keyword contract.
            pass

    async def scenario(endpoint):
        config = BuiltinConfig(
            database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'rerank.db'}"),
            runtime=RuntimeConfig(memory_rerank_candidate_limit=10),
        )
        async with httpx.AsyncClient(
            timeout=5, trust_env=False, headers={"x-client-header": "native-runtime"}
        ) as client:
            reranker = HTTPMemoryReranker(endpoint, client=client, batch_size=2)
            async with open_builtin_runtime(config, memory_reranker=reranker) as runtime:
                assert runtime.scopes is not None
                scope = await runtime.scopes.create(
                    ScopeDraft(title="HTTP rerank", summary="Exact evidence", idempotency_key="http-rerank")
                )
                memory = runtime.memory.for_scope(scope.scope_id)
                written = await memory.remember(
                    RememberMemoryRequest(entries=tuple(MemoryEntryInput(kind="fact", text=text) for text in scores))
                )
                result = await memory.search(SearchMemoryRequest(query="project", mode="fts", limit=3))
                trace = result.rerank
                assert trace is not None
                assert len(trace.candidate_hits) == len(scores)
                expected_ranks = tuple(
                    sorted(
                        range(1, len(trace.candidate_hits) + 1),
                        key=lambda rank: (-scores[trace.candidate_hits[rank - 1].text], rank),
                    )[:3]
                )
                assert trace.selected_ranks == expected_ranks
                assert trace.usage.requests == len(requests) == 3
                assert result.hits == tuple(trace.candidate_hits[rank - 1] for rank in expected_ranks)
                assert result.memory_ref == written.memory_ref
                assert all(hit.memory_ref == written.memory_ref for hit in result.hits)
                assert [row for request in requests for row in request["candidates"]] == [
                    {"rank": rank, "text": hit.text} for rank, hit in enumerate(trace.candidate_hits, 1)
                ]
                before = []
                for hit in trace.candidate_hits:
                    exact = await memory.get(
                        GetMemoryEntryRequest(
                            citation=MemoryCitation(
                                memory_ref=hit.memory_ref,
                                entry_id=hit.entry_id,
                                entry_version_id=hit.entry_version_id,
                            )
                        )
                    )
                    assert exact.entry.text == hit.text
                    before.append(exact)
            assert not client.is_closed
            async with open_builtin_runtime(config, memory_reranker=reranker) as runtime:
                memory = runtime.memory.for_scope(scope.scope_id)
                for original in before:
                    assert await memory.get(GetMemoryEntryRequest(citation=original.citation)) == original
                restarted = await memory.search(SearchMemoryRequest(query="project", mode="fts", limit=3))
                assert restarted.rerank is not None
                assert restarted.hits == result.hits
                assert restarted.rerank.usage.requests == 3 and len(requests) == 6

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            asyncio.run(scenario(f"http://127.0.0.1:{server.server_port}/score"))
        finally:
            server.shutdown()
            thread.join(timeout=5)
