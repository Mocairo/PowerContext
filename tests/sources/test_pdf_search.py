# Copyright (c) 2026 OceanBase.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software distributed
# under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
# CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import cast

import pytest

from powercontext.artifacts.models import ArtifactRef
from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.runtime.application import ScopedMemoryApplication
from powercontext.sources._pdf_search import search_pdf_pages
from powercontext.sources.models import SourceRef


class ScopedSearch:
    """A lexical public-API double; real database coverage lives in e2e tests."""

    def __init__(self, entries):
        self.entries = entries
        self.read = []

    async def search(self, request):
        assert request.mode == "fts"
        terms = set(re.findall(r"[a-z]+|\d+", request.query.lower()))
        return SimpleNamespace(
            hits=tuple(
                hit
                for hit, _sources, _state in self.entries
                if terms.intersection(re.findall(r"[a-z]+|\d+", hit.text.lower()))
            )[: request.limit]
        )

    async def get(self, request):
        for hit, sources, state in self.entries:
            if hit.entry_version_id == request.citation.entry_version_id:
                assert request.citation.memory_ref == hit.memory_ref
                assert request.citation.entry_id == hit.entry_id
                self.read.append(request.citation)
                return SimpleNamespace(state=state, entry=SimpleNamespace(text=hit.text, sources=sources))
        raise AssertionError("fabricated citation")  # noqa: TRY003


def entry(number, text, source_id=None, source_type="pdf-page", state="active"):
    hit = MemoryHit(
        memory_ref=ArtifactRef(family="memory", artifact_id="scoped-memory", revision=7),
        entry_id=f"entry-{number}",
        entry_version_id=f"version-{number}",
        text=text,
        score=1.0,
        matched_by=("fts",),
    )
    return hit, (SourceRef(source_type=source_type, source_id=source_id or f"page-{number}"),), state


@pytest.mark.asyncio
async def test_synonym_query_retrieves_legal_evidence_without_literal_wording():
    memory = ScopedSearch([
        entry(1, "NORTHSTAR_2031_10K\nLitigation contingencies: Claims totaled 173 dollars."),
        entry(2, "NORTHSTAR_2031_10K\nEmployee charitable activities and office locations."),
    ])
    citations = await search_pdf_pages(
        cast(ScopedMemoryApplication, memory), "Does Northstar have ongoing legal battles in FY2031?", limit=1
    )
    assert [citation.entry_id for citation in citations] == ["entry-1"]
    assert citations[0] in memory.read


@pytest.mark.asyncio
async def test_company_and_year_disambiguate_identical_tables():
    memory = ScopedSearch([
        entry(1, "OTHER_2034_10K\nNet sales revenue 800 operating income 100."),
        entry(2, "STARLING_2033_10K\nNet sales revenue 600 operating income 90."),
        entry(3, "STARLING_2034_10K\nNet sales revenue 700 operating income 95."),
    ])
    citations = await search_pdf_pages(
        cast(ScopedMemoryApplication, memory), "What is Starling's sales revenue for FY2034?", limit=1
    )
    assert [citation.entry_id for citation in citations] == ["entry-3"]


@pytest.mark.asyncio
@pytest.mark.parametrize("document_id,company", [("ORIONGROUP_2031_10K", "Orion-Group"), ("3M_2031_10K", "3M")])
async def test_explicit_document_name_survives_other_company_candidate_crowding(document_id, company):
    memory = ScopedSearch([
        *(
            entry(i, f"OTHER_2031_10K\nStatements of income\nNet income {i}\nNet earnings {i}\nTotal assets {i}")
            for i in range(25)
        ),
        entry(25, f"{document_id}\nNet income 10\nAssets 50"),
    ])
    citations = await search_pdf_pages(
        cast(ScopedMemoryApplication, memory), f"What figures determine {company}'s return on assets in 2031?", limit=1
    )
    assert [citation.entry_id for citation in citations] == ["entry-25"]


@pytest.mark.asyncio
async def test_document_names_match_whole_query_tokens_not_substrings():
    memory = ScopedSearch([
        entry(1, "TECH_2031_10K\nTechnical designs"),
        entry(2, "ORION_2031_10K\nTechnical designs describe hydraulic interfaces"),
    ])
    citations = await search_pdf_pages(
        cast(ScopedMemoryApplication, memory), "Which technical designs describe hydraulic interfaces?", limit=1
    )
    assert [citation.entry_id for citation in citations] == ["entry-2"]


@pytest.mark.asyncio
async def test_comparison_can_select_both_named_documents():
    memory = ScopedSearch([
        entry(1, "OTHER_2031_10K\nStatements of income\nNet income 900\nNet earnings 900\nTotal assets 900"),
        entry(2, "ORIONGROUP_2031_10K\nNet income 10\nTotal assets 50"),
        entry(3, "LYRA_2031_10K\nNet income 20\nTotal assets 80"),
    ])
    citations = await search_pdf_pages(
        cast(ScopedMemoryApplication, memory), "Compare Orion Group and Lyra return on assets for 2031", limit=2
    )
    assert {citation.entry_id for citation in citations} == {"entry-2", "entry-3"}


@pytest.mark.asyncio
async def test_duplicates_filtered_but_revisions_and_documents_retained():
    memory = ScopedSearch([
        entry(1, "ALPHA\nOperating income 901", "alpha-revision-a-page-1"),
        entry(2, "ALPHA\nOperating income 901", "alpha-revision-a-page-1"),
        entry(3, "ALPHA\nOperating income 902", "alpha-revision-b-page-1"),
        entry(4, "BETA\nOperating income 903", "beta-revision-a-page-1"),
        entry(5, "ALPHA\nOperating income 904", source_type="csv-row"),
        entry(6, "ALPHA\nOperating income 905", state="retired"),
    ])
    citations = await search_pdf_pages(cast(ScopedMemoryApplication, memory), "operating income", limit=20)
    assert {citation.entry_id for citation in citations} == {"entry-1", "entry-3", "entry-4"}
    assert all(citation in memory.read for citation in citations)


@pytest.mark.asyncio
async def test_limit_zero_empty_scope_and_unmatched_query():
    memory = ScopedSearch([])
    assert await search_pdf_pages(cast(ScopedMemoryApplication, memory), "sales", limit=0) == ()
    assert await search_pdf_pages(cast(ScopedMemoryApplication, memory), "sales") == ()
    memory = ScopedSearch([entry(1, "ALPHA\nOffice locations")])
    assert await search_pdf_pages(cast(ScopedMemoryApplication, memory), "zebrafish") == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [-1, 1.5, True])
async def test_invalid_limit(limit):
    with pytest.raises(ValueError):
        await search_pdf_pages(cast(ScopedMemoryApplication, ScopedSearch([])), "sales", limit=limit)


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["", " \t\n", None])
async def test_invalid_question(question):
    with pytest.raises(ValueError):
        await search_pdf_pages(cast(ScopedMemoryApplication, ScopedSearch([])), question)
