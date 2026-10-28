"""PDF evidence keeps exact Unicode text through native Memory and its hashes."""

from __future__ import annotations

import io
import json
import unicodedata

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.artifacts.memory.canonical import entry_content_bytes, entry_content_hash, normalize_text
from powercontext.builtin.runtime import (
    GetMemoryEntryRequest,
    RememberMemoryRequest,
    ReviseMemoryEntryRequest,
    open_builtin_runtime,
)
from powercontext.limits import MAX_SOURCE_OBSERVATION_BYTES
from powercontext.sources.pdf_pages import PDFDocumentInput, parse_pdf_document, search_pdf_pages
from tests.e2e.test_pdf_ingestion import _config, _inspect, _load
from tests.pdf_test_support import pdf_bytes

_PDF_REFS = ({"source_type": "pdf-page", "source_id": "pdf:test"},)


def _unicode_pdf(unicode_hex: str) -> bytes:
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_bytes(("CafA revenue 12345",)))))
    mapping = DecodedStreamObject()
    mapping.set_data(
        f"""/CIDInit /ProcSet findresource begin
12 dict begin begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /ReportUnicode def /CMapType 2 def
1 begincodespacerange <00> <FF> endcodespacerange
1 beginbfchar <41> <{unicode_hex}> endbfchar
endcmap CMapName currentdict /CMap defineresource pop end end
""".encode("ascii")
    )
    resources = writer.pages[0]["/Resources"]
    assert isinstance(resources, DictionaryObject)
    fonts = resources["/Font"]
    assert isinstance(fonts, DictionaryObject)
    font = fonts["/F1"]
    assert isinstance(font, DictionaryObject)
    font[NameObject("/ToUnicode")] = writer._add_object(mapping)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


async def test_unicode_pdf_source_and_memory_match_after_restart_and_revision(tmp_path):
    pages = tuple(
        parse_pdf_document(PDFDocumentInput(document_id="Cafe\u0301_REPORT", content=_unicode_pdf(mapping)))[0]
        for mapping in ("00650301", "00E9")
    )
    assert pages[0].text == "Cafe\u0301 revenue 12345"
    assert pages[1].text == "Café revenue 12345"
    assert pages[0].document_sha256 != pages[1].document_sha256
    database = tmp_path / "unicode.db"
    (scope,) = await _load(database, (pages,))

    # This helper reopens the database and checks exact Source projection equality
    # for every citation, matching the official evidence readback contract.
    inspected = await _inspect(database, scope, (("revenue", 20),))
    assert len(inspected["results"][0]) == 2
    async with open_builtin_runtime(_config(database)) as runtime:
        memory = runtime.memory.for_scope(scope)
        citations = await search_pdf_pages(memory, "revenue", limit=20)
        records = [await memory.get(GetMemoryEntryRequest(citation=citation)) for citation in citations]
        assert {record.entry.text for record in records} == {f"{page.document_id}\n{page.text}" for page in pages}
        before = next(record for record in records if "Cafe\u0301 revenue" in record.entry.text)
        changed_text = before.entry.text.replace("Cafe\u0301 revenue", "Café revenue")
        changed = await memory.revise(
            ReviseMemoryEntryRequest(citation=before.citation, kind=before.entry.kind, text=changed_text)
        )
        assert changed.entry is not None
        assert changed.entry.entry.sources == before.entry.sources
        assert changed.entry.entry.entry_content_hash != before.entry.entry_content_hash
        assert changed.entry.entry.entry_version_id != before.entry.entry_version_id
        ordinary = await memory.remember(
            RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text="ordinary Cafe\u0301 note"),))
        )
        assert ordinary.entry is not None
        assert ordinary.entry.entry.text == "ordinary Café note"

    async with open_builtin_runtime(_config(database)) as runtime:
        memory = runtime.memory.for_scope(scope)
        assert (await memory.get(GetMemoryEntryRequest(citation=before.citation))).entry == before.entry
        assert (await memory.get(GetMemoryEntryRequest(citation=changed.entry.citation))).entry.text == changed_text


def test_pdf_content_hash_distinguishes_exact_unicode_body():
    decomposed, composed = "Annual report\nCafe\u0301 revenue 12345", "Annual report\nCafé revenue 12345"
    assert unicodedata.normalize("NFC", decomposed) == composed
    payload = entry_content_bytes(kind="source-evidence", text=decomposed, source_refs=_PDF_REFS, artifact_refs=())
    assert json.loads(payload)["text"] == decomposed
    assert entry_content_hash(
        kind="source-evidence", text=decomposed, source_refs=_PDF_REFS, artifact_refs=()
    ) != entry_content_hash(kind="source-evidence", text=composed, source_refs=_PDF_REFS, artifact_refs=())


def test_existing_nfc_pdf_content_hash_remains_compatible():
    # Frozen from the submitted b60d685 implementation, before this repair.
    assert (
        entry_content_hash(
            kind="source-evidence", text="Annual report\nCafé revenue 12345", source_refs=_PDF_REFS, artifact_refs=()
        )
        == "e61e401f69eaef6c6f466ab831c63f35bf2b6d3a79e9277455f2655ec95426b6"
    )


@pytest.mark.parametrize(
    "sources",
    [
        (),
        ({"source_type": "csv-row", "source_id": "row"},),
        (*_PDF_REFS, {"source_type": "csv-row", "source_id": "row"}),
    ],
)
def test_non_pdf_and_mixed_sources_keep_nfc_and_original_hash_behavior(sources):
    assert normalize_text(" Cafe\u0301 ", source_refs=sources) == "Café"
    assert entry_content_hash(
        kind="fact", text="Cafe\u0301", source_refs=sources, artifact_refs=()
    ) == entry_content_hash(kind="fact", text="Café", source_refs=sources, artifact_refs=())


def test_pdf_limit_counts_original_unicode_bytes():
    text = "e\u0301" * (MAX_SOURCE_OBSERVATION_BYTES // 3 + 1)
    assert len(text.encode("utf-8")) > MAX_SOURCE_OBSERVATION_BYTES
    assert len(unicodedata.normalize("NFC", text).encode("utf-8")) < MAX_SOURCE_OBSERVATION_BYTES
    with pytest.raises(ValueError, match="4194304"):
        normalize_text(text, source_refs=_PDF_REFS)
