"""Reproducible offline CSV parse/projection benchmark; no competition score is inferred."""

from __future__ import annotations

import argparse
import asyncio
import csv
import gc
import hashlib
import io
import json
import resource
import statistics
import time
from collections.abc import Sequence

from powercontext.sources import SourceDefinitionRegistry
from powercontext.sources.csv_rows import CSV_ROW_SOURCE_DEFINITION, CSVDocumentInput, CSVRowInput, parse_csv_document
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


def record(index: int, width: int) -> tuple[str, ...]:
    return (
        f" key:{index:08d}/ ",
        *(
            ("", "001.00", "中文 café", 'comma, quote"', "line1\r\nline2", f"value-{index}-{column}")[column % 6]
            for column in range(1, width)
        ),
    )


async def projections(rows: Sequence[CSVRowInput]) -> None:
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    for row in rows:
        source = await registry.resolve(row)
        evidence = registry.project(source, TEXT_EVIDENCE_PROJECTION_KEY)
        expected = dict(zip(row.columns, row.values, strict=True))
        if not isinstance(evidence, dict) or evidence["content"] != json.dumps(
            expected, ensure_ascii=False, separators=(",", ":")
        ):
            raise AssertionError("projection changed record contents or column order")  # noqa: TRY003
        if await registry.read(source) != row:
            raise AssertionError("Source read did not return the original row")  # noqa: TRY003


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--columns", type=int, default=20)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--project", type=int, default=10_000)
    parser.add_argument("--document-id", default="benchmark:机场/记录")
    args = parser.parse_args()
    output = io.StringIO(newline="")
    columns = (" id ", *(f"column-{i}" for i in range(1, args.columns)))
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(columns)
    writer.writerows(record(i, args.columns) for i in range(args.rows))
    content = output.getvalue().encode("utf-8-sig")
    output.close()
    document = CSVDocumentInput(document_id=args.document_id, primary_key=" id ", content=content)
    times = []
    for _ in range(args.repeats):
        gc.collect()
        started = time.perf_counter()
        rows = parse_csv_document(document)
        times.append(time.perf_counter() - started)
        if len(rows) != args.rows:
            raise AssertionError("row count changed")  # noqa: TRY003
        for i, row in enumerate(rows):
            if row.columns != columns or row.values != record(i, args.columns):
                raise AssertionError(f"record {i} changed")  # noqa: TRY003
        del rows
    rows = parse_csv_document(document)
    started = time.perf_counter()
    asyncio.run(projections(rows[: args.project]))
    projection_seconds = time.perf_counter() - started
    print(
        json.dumps(
            {
                "rows": args.rows,
                "columns": args.columns,
                "input_bytes": len(content),
                "input_sha256": hashlib.sha256(content).hexdigest(),
                "parse_seconds": times,
                "parse_median_seconds": statistics.median(times),
                "projection_rows": min(args.project, len(rows)),
                "projection_and_read_seconds": projection_seconds,
                "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "correctness": "every field of every parsed row checked on every repeat; projected rows read back exactly",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
