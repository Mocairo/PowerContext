"""Measure one offline Pi parse in a fresh process and verify every original record.

Run repeatedly in separate interpreters for comparable peak RSS values. This measures
parsing only, not Runtime ingestion or competition scoring.
"""

from __future__ import annotations

import argparse
import cProfile
import gc
import hashlib
import importlib.util
import io
import json
import platform
import resource
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import cast

from powercontext.sources import _pi_session_parser


def records(events: int) -> Iterator[tuple[dict[str, object], str | None]]:
    parent = None
    for index in range(events):
        if index and index % 10 == 0:
            control = f"control-{index}"
            yield {"type": "model_change", "id": control, "parentId": parent, "model": "offline"}, None
            parent = control
        event_id = f"event-{index}"
        text = f"evidence {index} 中文\n" + "original payload " * 24
        record: dict[str, object] = {"id": event_id, "parentId": parent}
        if index % 4 == 0:
            record.update(type="message", message={"role": "user", "content": text})
            expected = f"user: {text}"
        elif index % 4 == 1:
            arguments = {"path": f"资料/{index}.txt", "options": {"offset": index, "flags": [True, None, "中文"]}}
            record.update(
                type="message",
                message={
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": text},
                        {"type": "thinking", "thinking": "not evidence"},
                        {"type": "toolCall", "name": "read", "id": f"call-{index}", "arguments": arguments},
                    ],
                },
            )
            args = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            expected = f"assistant: {text}\n[tool_call read call-{index}] {args}"
        elif index % 4 == 2:
            record.update(
                type="message",
                message={
                    "role": "toolResult",
                    "toolName": "read",
                    "toolCallId": f"call-{index - 1}",
                    "content": [{"type": "text", "text": text}, {"type": "image", "data": "opaque"}],
                },
            )
            expected = f"toolResult: [tool_result read call-{index - 1}] {text}"
        else:
            record.update(type="compaction", summary=text, firstKeptEntryId=f"event-{index - 2}")
            expected = f"compaction: {text}"
        record["usage"] = {"input": index, "cached": 0, "nested": [{"labels": ["原始", "trace"], "cost": 0.125}]}
        yield record, expected
        parent = event_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=10_000)
    parser.add_argument("--parser-file", type=Path, help="Load a saved parser snapshot for before/after comparisons")
    parser.add_argument("--profile", type=Path, help="Write cProfile data for the parse interval only")
    args = parser.parse_args()
    if args.events < 1:
        parser.error("--events must be positive")
    module = _pi_session_parser
    if args.parser_file is not None:
        spec = importlib.util.spec_from_file_location("pi_benchmark_snapshot", args.parser_file)
        if spec is None or spec.loader is None:
            parser.error("cannot load --parser-file")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    source_path = Path(cast(str, module.__file__))
    output = io.StringIO()
    output.write('{"type":"session","version":3,"id":"benchmark-session"}\n')
    for record, _ in records(args.events):
        output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    content = output.getvalue().encode()
    output.close()
    value = module.PiSessionInput(content=content)
    gc.collect()
    profiler = cProfile.Profile() if args.profile else None
    if profiler:
        profiler.enable()
    started = time.perf_counter()
    events = module.parse_pi_session(value)
    elapsed = time.perf_counter() - started
    if profiler:
        profiler.disable()
        profiler.dump_stats(args.profile)
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if len(events) != args.events:
        raise AssertionError("visible event count changed")  # noqa: TRY003
    expected = ((record, body) for record, body in records(args.events) if body is not None)
    for event, (record, body) in zip(events, expected, strict=True):
        if event.record != record or event.content != body or event.session_id != "benchmark-session":
            raise AssertionError(f"original evidence changed for {event.event_id}")  # noqa: TRY003
    print(
        json.dumps(
            {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "visible_events": args.events,
                "input_bytes": len(content),
                "input_sha256": hashlib.sha256(content).hexdigest(),
                "parser_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                "parse_seconds": elapsed,
                "max_rss_kib": peak_rss,
                "correctness": "every visible event body, complete original record and session identity verified",
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
