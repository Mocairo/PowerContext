"""PDF evidence through real ingestion, durable Memory and a fresh Python process."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    SubmitSourceObservation,
    open_builtin_contexts,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import (
    SourceDefinitionRegistry,
    SourceObservation,
    TextEvidence,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    PDFPageInput,
    parse_pdf_document,
    search_pdf_pages,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY
from tests.pdf_test_support import pdf_bytes, terms


class _PagePipeline:
    async def extract(self, request, /):
        return tuple(
            MemoryEntryInput(
                kind="pdf-evidence",
                text=source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"],
                sources=(source,),
            )
            for source in request.sources
        )


def _config(database):
    return BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{database}"))


def _report(company, year, marker):
    pages = [
        f"{company} {year} commonmarker\nFactory safety policies and employee training section {i}." for i in range(12)
    ]
    pages[1] = f"{company} {year} commonmarker\nGeographic segments: Europe, Asia Pacific and North America."
    pages[3] = (
        f"{company} {year} commonmarker\nConsolidated income statements\nRevenue 12345\nOperating income 3456\n{marker}"
    )
    pages[5] = (
        f"{company} {year} commonmarker\nConsolidated statements of cash flows\nDepreciation and amortization 567\nCapital expenditures 890"
    )
    pages[8] = f"{company} {year} commonmarker\nLegal proceedings: patent litigation remains pending."
    pages[10] = ""
    return tuple(pages)


async def _load(database, scopes_and_pages):
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    scopes = []
    async with open_builtin_runtime(_config(database)) as runtime:
        await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
        assert runtime.scopes is not None
        for index, pages in enumerate(scopes_and_pages):
            scope = await runtime.scopes.create(
                ScopeDraft(
                    title=f"Report scope {index}", summary="Synthetic PDF evidence", idempotency_key=f"pdf-{index}"
                )
            )
            scopes.append(scope.scope_id)
            observations = [project_source_for_transport(registry, await registry.resolve(page)) for page in pages]
            for _ in range(2):
                for observation in observations:
                    await runtime.ingestion.submit(
                        SubmitSourceObservation(scope_id=scope.scope_id, observation=observation)
                    )
    async with open_builtin_runtime(_config(database), candidate_pipeline=_PagePipeline()) as runtime:
        for scope_id, pages in zip(scopes, scopes_and_pages, strict=True):
            memory = runtime.memory.for_scope(scope_id)
            for _ in range(len(pages) + 1):
                if not (await memory.flush()).processed:
                    break
            else:
                raise AssertionError("PDF pipeline did not drain")  # noqa: TRY003
            assert not (await memory.flush()).processed
            assert len((await memory.list()).entries) == len(pages)
    return scopes


async def _inspect(database, scope_id, queries):
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    async with (
        open_builtin_runtime(_config(database)) as runtime,
        open_builtin_contexts(_config(database)) as contexts,
    ):
        context = await contexts.get(scope_id)
        journal = await context.sources.journal.entries()
        stored = {entry.source_ref.source_id: entry.source for entry in journal}
        decoded: dict[str, PDFPageInput] = {}
        for name, observation in stored.items():
            persisted = await context.sources.catalog.get(observation)
            assert isinstance(persisted, SourceObservation)
            native = PDF_PAGE_SOURCE_DEFINITION.source_class.model_validate(persisted.payload)
            page = await registry.read(native)
            assert isinstance(page, PDFPageInput)
            decoded[name] = page
        memory = runtime.memory.for_scope(scope_id)
        results = []
        for question, limit in queries:
            citations = await search_pdf_pages(memory, question, limit=limit)
            assert isinstance(citations, tuple) and len(citations) <= limit
            locations = []
            for citation in citations:
                assert citation.entry_version_id
                entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
                assert len(entry.sources) == 1
                reference = entry.sources[0]
                assert reference.source_type == "pdf-page"
                source = stored[reference.source_id]
                assert isinstance(source, SourceObservation)
                evidence = TextEvidence.model_validate(source.projection(TEXT_EVIDENCE_PROJECTION_KEY))
                page = decoded[reference.source_id]
                assert entry.text == evidence.content.strip()
                assert evidence.metadata == {
                    "document_id": page.document_id,
                    "document_sha256": page.document_sha256,
                    "page_number": page.page_number,
                }
                locations.append([page.document_id, page.document_sha256, page.page_number])
            assert len({tuple(location) for location in locations}) == len(locations)
            results.append(locations)
        return {"pages": [page.model_dump() for page in decoded.values()], "results": results}


_PROCESS_INSPECT = """
import asyncio
import json
import runpy
import sys
from pathlib import Path
helpers = runpy.run_path(sys.argv[1])
request = json.loads(Path(sys.argv[2]).read_text())
result = asyncio.run(helpers['_inspect'](request['database'], request['scope_id'], request['queries']))
Path(sys.argv[3]).write_text(json.dumps(result))
"""


def test_all_pages_replay_revision_scope_and_fresh_process_retrieval(tmp_path):
    original = parse_pdf_document(
        PDFDocumentInput(
            document_id="ASTER_2031", content=pdf_bytes(_report("Aster", 2031, "legacywidget versionmarker"))
        )
    )
    revised = parse_pdf_document(
        PDFDocumentInput(
            document_id="ASTER_2031",
            content=pdf_bytes(_report("Aster", 2031, "modernwidget versionmarker"), title="Correction"),
        )
    )
    other = parse_pdf_document(
        PDFDocumentInput(
            document_id="BIRCH_2028", content=pdf_bytes(_report("Birch", 2028, "birchwidget versionmarker"))
        )
    )
    private = parse_pdf_document(
        PDFDocumentInput(
            document_id="PRIVATE_2040", content=pdf_bytes(("confidentialscope secretwidget versionmarker",))
        )
    )
    pages = original + revised + other
    database = tmp_path / "pdf.db"
    scope, private_scope = asyncio.run(_load(database, (pages, private)))
    queries = [
        ["Where is legacywidget reported?", 1],
        ["Where is modernwidget reported?", 1],
        ["Where is birchwidget reported?", 1],
        ["versionmarker", 20],
        ["commonmarker", 3],
        ["commonmarker", 1],
        ["commonmarker", 0],
        ["secretwidget", 20],
        ["What geographic segments does Birch operate in?", 20],
        ["Which legal proceedings describe patent litigation for Birch?", 20],
        ["What are Birch depreciation and amortization and capital expenditures?", 20],
        ["What is Birch operating income and revenue?", 20],
    ]
    before = asyncio.run(_inspect(database, scope, queries))
    assert len(before["pages"]) == 36
    expected_pages = {(page.document_id, page.document_sha256, page.page_number): page for page in pages}
    for restored in before["pages"]:
        key = (restored["document_id"], restored["document_sha256"], restored["page_number"])
        assert restored == expected_pages[key].model_dump()
    request, output = tmp_path / "request.json", tmp_path / "response.json"
    request.write_text(json.dumps({"database": str(database), "scope_id": scope, "queries": queries}))
    subprocess.run(
        [sys.executable, "-c", _PROCESS_INSPECT, __file__, str(request), str(output)],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    after = json.loads(output.read_text())
    assert after == before
    results = after["results"]
    for index, expected in enumerate((original[3], revised[3], other[3])):
        assert results[index] == [[expected.document_id, expected.document_sha256, expected.page_number]]
    assert {tuple(location) for location in results[3]} == {
        (page.document_id, page.document_sha256, page.page_number) for page in (original[3], revised[3], other[3])
    }
    assert len(results[4]) == 3 and len(results[5]) == 1 and results[6] == []
    assert not any(location[0] == "PRIVATE_2040" for locations in results for location in locations)
    private_results = asyncio.run(_inspect(database, private_scope, [["secretwidget", 20]]))
    assert len(private_results["pages"]) == 1
    assert private_results["results"] == [[[private[0].document_id, private[0].document_sha256, 1]]]
    # Synthetic recall uses document AND physical page, and sums distinct gold pages per query.
    expected_evidence = [{("BIRCH_2028", number)} for number in (2, 9, 6, 4)]
    hits = sum(
        len({(location[0], location[2]) for location in actual} & gold)
        for actual, gold in zip(results[8:], expected_evidence, strict=True)
    )
    total = sum(len(gold) for gold in expected_evidence)
    assert hits / total >= 0.70
    assert all(
        terms(page["text"])
        == terms(expected_pages[(page["document_id"], page["document_sha256"], page["page_number"])].text)
        for page in after["pages"]
    )
    print(
        json.dumps({
            "dataset": "self_constructed_not_FinanceBench",
            "pages": 36,
            "isolated_scope_pages": 1,
            "fresh_process": True,
            "evidence_pages_hit": hits,
            "evidence_pages_total": total,
            "recall_at_20": hits / total,
        })
    )


def test_citation_issued_before_revision_import_still_reads_original_after_restart(tmp_path):
    async def scenario():
        original = parse_pdf_document(
            PDFDocumentInput(document_id="same-report", content=pdf_bytes(("Originalmarker revenue 100",)))
        )
        revised = parse_pdf_document(
            PDFDocumentInput(document_id="same-report", content=pdf_bytes(("Revisedmarker revenue 200",)))
        )
        database = tmp_path / "revisions.db"
        scope_id = (await _load(database, (original,)))[0]
        async with open_builtin_runtime(_config(database)) as runtime:
            memory = runtime.memory.for_scope(scope_id)
            old_citations = await search_pdf_pages(memory, "Originalmarker revenue", limit=1)
            assert len(old_citations) == 1
            old_entry = await memory.get(GetMemoryEntryRequest(citation=old_citations[0]))
        registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
        observation = project_source_for_transport(registry, await registry.resolve(revised[0]))
        async with open_builtin_runtime(_config(database), candidate_pipeline=_PagePipeline()) as runtime:
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            await runtime.ingestion.submit(SubmitSourceObservation(scope_id=scope_id, observation=observation))
            memory = runtime.memory.for_scope(scope_id)
            assert (await memory.flush()).processed
            assert not (await memory.flush()).processed
        async with open_builtin_runtime(_config(database)) as runtime:
            memory = runtime.memory.for_scope(scope_id)
            assert await memory.get(GetMemoryEntryRequest(citation=old_citations[0])) == old_entry
            citations = await search_pdf_pages(memory, "Revisedmarker revenue", limit=1)
            assert len(citations) == 1
            updated_entry = await memory.get(GetMemoryEntryRequest(citation=citations[0]))
            assert updated_entry.entry.text == "same-report\nRevisedmarker revenue 200"
            assert updated_entry.entry.sources != old_entry.entry.sources
            assert citations[0].memory_ref != old_citations[0].memory_ref
        all_pages = (await _inspect(database, scope_id, [["revenue", 20]]))["pages"]
        assert {page["document_sha256"] for page in all_pages} == {
            original[0].document_sha256,
            revised[0].document_sha256,
        }

    asyncio.run(scenario())
