"""Dense PDF pages retain their complete text without relaxing ordinary Memory limits."""

from __future__ import annotations

import pytest

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.artifacts.memory.canonical import entry_content_hash, normalize_text
from powercontext.builtin.artifacts.memory.errors import InvalidMemoryCandidateError
from powercontext.builtin.runtime import (
    GetMemoryEntryRequest,
    RememberMemoryRequest,
    SearchMemoryRequest,
    open_builtin_runtime,
)
from powercontext.limits import MAX_SOURCE_OBSERVATION_BYTES
from powercontext.sources.pdf_pages import PDFDocumentInput, parse_pdf_document
from tests.e2e.test_pdf_ingestion import _config, _load
from tests.pdf_test_support import pdf_bytes


async def test_dense_page_round_trip_and_exact_hash_after_reopening(tmp_path):
    text = "\n".join(f"Dense financial table row {i}: revenue 12345 expenses 6789." for i in range(180))
    pages = parse_pdf_document(PDFDocumentInput(document_id="dense-report", content=pdf_bytes((text,))))
    expected = f"dense-report\n{pages[0].text}".strip()
    assert len(expected.encode("utf-8")) > 8192
    database = tmp_path / "dense.db"
    (scope_id,) = await _load(database, (pages,))
    async with open_builtin_runtime(_config(database)) as runtime:
        memory = runtime.memory.for_scope(scope_id)
        entries = (await memory.list()).entries
        assert len(entries) == 1
        record = entries[0]
        assert record.entry.text == expected
        hits = (await memory.search(SearchMemoryRequest(query="financial", mode="fts", limit=2))).hits
        assert len(hits) == 1
        citation = record.citation
        assert hits[0].entry_version_id == citation.entry_version_id
        assert record.entry.entry_content_hash == entry_content_hash(
            kind=record.entry.kind,
            text=expected,
            source_refs=tuple(source.model_dump(mode="json") for source in record.entry.sources),
            artifact_refs=tuple(artifact.model_dump(mode="json") for artifact in record.entry.artifacts),
        )
        with pytest.raises(InvalidMemoryCandidateError, match="8192"):
            await memory.remember(RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text="x" * 8193),)))
    async with open_builtin_runtime(_config(database)) as runtime:
        restored = await runtime.memory.for_scope(scope_id).get(GetMemoryEntryRequest(citation=citation))
        assert restored == record


def test_ordinary_memory_keeps_original_byte_limit():
    with pytest.raises(ValueError, match="8192"):
        normalize_text("x" * 8193)
    with pytest.raises(ValueError, match="8192"):
        entry_content_hash(kind="fact", text="x" * 8193, source_refs=(), artifact_refs=())
    with pytest.raises(ValueError, match="8192"):
        entry_content_hash(
            kind="fact", text="x" * 8193, source_refs=({"source_type": "csv-row", "source_id": "x"},), artifact_refs=()
        )
    with pytest.raises(ValueError, match="8192"):
        entry_content_hash(
            kind="fact",
            text="x" * 8193,
            source_refs=(
                {"source_type": "pdf-page", "source_id": "pdf:x"},
                {"source_type": "csv-row", "source_id": "csv:x"},
            ),
            artifact_refs=(),
        )


def test_pdf_memory_remains_bounded_by_source_observation_budget():
    with pytest.raises(ValueError, match="4194304"):
        entry_content_hash(
            kind="source-evidence",
            text="x" * (MAX_SOURCE_OBSERVATION_BYTES + 1),
            source_refs=({"source_type": "pdf-page", "source_id": "pdf:x"},),
            artifact_refs=(),
        )
