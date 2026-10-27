"""Reproducible full-report verification, without FinanceBench answer annotations.

Run with PYTHONPATH=src:. competition/.venv/bin/python -m tests.validate_pdf_public
WORKLOAD_DIRECTORY OUTPUT_DIRECTORY. Output contains a database and JSON evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

from powercontext.artifacts.models import MemoryCitation
from powercontext.builtin.runtime import GetMemoryEntryRequest, open_builtin_runtime
from powercontext.sources.pdf_pages import PDFDocumentInput, parse_pdf_document
from tests.e2e.test_pdf_ingestion import _config, _inspect, _load
from tests.pdf_test_support import terms


async def verify(request):
    """This phase runs after the writer process has discarded every parsed page."""
    queries = [(item["question"], 20) for item in request["questions"]]
    result = await _inspect(request["database"], request["scope_id"], queries)
    expected = {(page["document_id"], page["document_sha256"], page["page_number"]): page for page in request["pages"]}
    restored = {(page["document_id"], page["document_sha256"], page["page_number"]): page for page in result["pages"]}
    assert restored == expected
    bodies = sorted(f"{page['document_id']}\n{page['text']}".strip() for page in request["pages"])
    async with open_builtin_runtime(_config(request["database"])) as runtime:
        memory = runtime.memory.for_scope(request["scope_id"])
        entries = (await memory.list()).entries
        assert len(entries) == len(expected)
        assert sorted(record.entry.text for record in entries) == bodies
        for record in entries:
            citation = MemoryCitation(
                memory_ref=record.memory_ref,
                entry_id=record.entry.entry_id,
                entry_version_id=record.entry.entry_version_id,
            )
            assert (await memory.get(GetMemoryEntryRequest(citation=citation))).entry == record.entry
    result["all_memory_entries_read_back"] = len(entries)
    result["questions"] = [
        {"question": item["question"], "locations": locations, "returned": len(locations)}
        for item, locations in zip(request["questions"], result.pop("results"), strict=True)
    ]
    result.pop("pages")
    return result


def main(workload: Path, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    database = output / "public.db"
    if database.exists():
        raise ValueError("Use a fresh output directory for an independent persistence run")  # noqa: TRY003
    cases = json.loads((workload / "cases.json").read_text())["cases"]
    pages, questions, document_metrics = [], [], []
    started = time.monotonic()
    for case in cases:
        raw = (workload / case["file"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == case["sha256"]
        parsed = parse_pdf_document(PDFDocumentInput(document_id=case["document_id"], content=raw))
        assert len(parsed) == case["page_count"]
        assert [page.page_number for page in parsed] == list(range(1, len(parsed) + 1))
        pages.extend(parsed)
        questions.extend(case["questions"])
        document_metrics.append({
            "document_id": case["document_id"],
            "sha256": case["sha256"],
            "pages": len(parsed),
            "empty_pages": [page.page_number for page in parsed if not page.text],
            "total_terms": sum(sum(terms(page.text).values()) for page in parsed),
            "max_page_utf8_bytes": max(len(page.text.encode()) for page in parsed),
            "page_statistics": [
                {"page": page.page_number, "terms": sum(terms(page.text).values()), "chars": len(page.text)}
                for page in parsed
            ],
        })
    parsed_at = time.monotonic()
    scope_id = asyncio.run(_load(database, (tuple(pages),)))[0]
    stored_at = time.monotonic()
    request_path = output / "expected.json"
    request_path.write_text(
        json.dumps({
            "database": str(database.resolve()),
            "scope_id": scope_id,
            "pages": [page.model_dump() for page in pages],
            "questions": questions,
        })
    )
    del pages
    subprocess.run(
        [sys.executable, "-m", "tests.validate_pdf_public", "--verify", str(request_path)],
        check=True,
    )
    verification = json.loads((output / "restart.json").read_text())
    summary = {
        "documents": document_metrics,
        "questions": len(questions),
        "all_pages_submitted_twice": True,
        "duplicate_sources": 0,
        "fresh_process": verification,
        "seconds": {
            "parse": parsed_at - started,
            "ingest_and_flush": stored_at - parsed_at,
            "fresh_process_verify": time.monotonic() - stored_at,
        },
        "quality_note": "No frozen per-page counts or FinanceBench evidence gold available; no official quality scores.",
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(
        json.dumps({
            "documents": len(cases),
            "pages": verification["all_memory_entries_read_back"],
            "questions": len(questions),
            "returned": [item["returned"] for item in verification["questions"]],
            "seconds": summary["seconds"],
        }),
        flush=True,
    )


if __name__ == "__main__":
    if sys.argv[1] == "--verify":
        request_path = Path(sys.argv[2])
        result = asyncio.run(verify(json.loads(request_path.read_text())))
        request_path.with_name("restart.json").write_text(json.dumps(result, indent=2))
    else:
        main(Path(sys.argv[1]), Path(sys.argv[2]))
