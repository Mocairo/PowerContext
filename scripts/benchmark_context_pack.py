"""Measure real Runtime context traffic using an offline, declared meeting-turn schedule.

The input JSON contains a ``turns`` array of original ``text`` values. This is a
local reproducible workload, not a reproduction of the private evaluation data.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

from powercontext.builtin.artifacts.memory import MemoryCandidateRequest, MemoryEntryInput
from powercontext.builtin.runtime import BuiltinConfig, CaptureSource, PrepareContextRequest, open_builtin_runtime
from powercontext.builtin.runtime.context_pack import ContextPack, ContextPackDelta, apply_context_pack_delta
from powercontext.builtin.scope import ScopeDraft
from powercontext.builtin.sources import ContentSource


class _Pipeline:
    async def extract(self, request: MemoryCandidateRequest, /) -> tuple[MemoryEntryInput, ...]:
        return tuple(
            MemoryEntryInput(kind="meeting-turn", text=source.content, sources=(source,))
            for source in request.sources
            if isinstance(source, ContentSource)
        )


async def measure(args: argparse.Namespace) -> dict[str, object]:
    data = args.turns_json.read_bytes()
    turns = json.loads(data)["turns"]
    if not 1 <= args.initial_turns <= len(turns):
        raise ValueError("initial-turns must fit the supplied original turns")  # noqa: TRY003
    if args.rounds < 1 or args.append_every < 1:
        raise ValueError("rounds and append-every must be positive")  # noqa: TRY003
    rows: list[dict[str, Any]] = []
    base_wire = None
    imported = 0
    async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_Pipeline()) as runtime:
        if runtime.scopes is None:
            raise RuntimeError("Runtime scopes are unavailable")  # noqa: TRY003
        scope = await runtime.scopes.create(
            ScopeDraft(title="Meeting", summary="Wire measurement", idempotency_key="wire")
        )
        sources = runtime.sources.for_scope(scope.scope_id)
        memory = runtime.memory.for_scope(scope.scope_id)
        context = runtime.context.for_scope(scope.scope_id)
        for round_index in range(args.rounds):
            stage = round_index // args.append_every
            required = min(args.initial_turns + stage, len(turns))
            for index in range(imported, required):
                await sources.capture(
                    CaptureSource(source_id=f"turn-{index}", content=turns[index]["text"], metadata={})
                )
            if required != imported:
                while (await memory.flush()).processed:
                    pass
                imported = required
            query = args.queries[min(stage, len(args.queries) - 1)]
            request = PrepareContextRequest(query=query, max_bytes=32768)
            sender_base = None if base_wire is None else ContextPack.from_bytes(base_wire)
            transmission = await context.prepare_incremental(request, sender_base)
            expected = await context.prepare(request)
            raw = (expected.content or "").encode()
            wire = transmission.to_bytes()
            if base_wire is None:
                received = ContextPack.from_bytes(wire)
            else:
                received = apply_context_pack_delta(
                    ContextPack.from_bytes(base_wire), ContextPackDelta.from_bytes(wire)
                )
            if b"".join(received.items) != raw:
                raise AssertionError("Independent wire receiver differs from ordinary prepare")  # noqa: TRY003
            base_wire = received.to_bytes()
            rows.append({
                "round": round_index,
                "query": query,
                "captured_turns": imported,
                "raw_full_bytes": len(raw),
                "full_pack_bytes": len(ContextPack.from_content(expected.content or "").to_bytes()),
                "gzip_bytes": len(gzip.compress(raw, compresslevel=9, mtime=0)),
                "incremental_bytes": len(wire),
                "body_sha256": hashlib.sha256(raw).hexdigest(),
                "packet_sha256": hashlib.sha256(wire).hexdigest(),
            })
    totals: dict[str, int] = {
        key: sum(row[key] for row in rows)
        for key in ("raw_full_bytes", "full_pack_bytes", "gzip_bytes", "incremental_bytes")
    }
    return {
        "note": "Declared local offline schedule; private competition workload was not available.",
        "input_sha256": hashlib.sha256(data).hexdigest(),
        "initial_turns": args.initial_turns,
        "append_every": args.append_every,
        "queries": args.queries,
        "rounds": len(rows),
        "nonempty_rounds": sum(row["raw_full_bytes"] > 0 for row in rows),
        "distinct_contexts": len({row["body_sha256"] for row in rows}),
        **totals,
        "savings_vs_raw": 1 - totals["incremental_bytes"] / totals["raw_full_bytes"]
        if totals["raw_full_bytes"]
        else None,
        "savings_vs_full_pack": 1 - totals["incremental_bytes"] / totals["full_pack_bytes"],
        "savings_vs_gzip": 1 - totals["incremental_bytes"] / totals["gzip_bytes"],
        "samples": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns-json", required=True, type=Path)
    parser.add_argument("--initial-turns", type=int, default=8)
    parser.add_argument("--rounds", type=int, default=40)
    parser.add_argument("--append-every", type=int, default=10)
    parser.add_argument("--queries", nargs="+", default=["remote"])
    args = parser.parse_args()
    print(json.dumps(asyncio.run(measure(args)), indent=2))


if __name__ == "__main__":
    main()
