# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Offline lexical PDF retrieval through the caller's scoped Memory API."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import TYPE_CHECKING

from powercontext.artifacts.models import MemoryCitation
from powercontext.builtin.runtime.models import GetMemoryEntryRequest, SearchMemoryRequest

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory.models import MemoryHit
    from powercontext.builtin.runtime.application import ScopedMemoryApplication

_WORD = re.compile(r"[a-z]+|\d+")
_STOP = frozenset([
    "a",
    "an",
    "the",
    "and",
    "or",
    "for",
    "of",
    "to",
    "in",
    "on",
    "at",
    "by",
    "from",
    "with",
    "as",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "what",
    "which",
    "who",
    "how",
    "why",
    "when",
    "where",
    "do",
    "does",
    "did",
    "can",
    "could",
    "should",
    "would",
    "has",
    "have",
    "had",
    "i",
    "we",
    "you",
    "it",
    "its",
    "our",
    "your",
    "this",
    "that",
    "these",
    "those",
    "s",
    "t",
    "give",
    "response",
    "respond",
    "answer",
    "question",
    "using",
    "use",
    "shown",
    "details",
    "assume",
    "assuming",
    "perspective",
    "investment",
    "analyst",
    "only",
    "please",
    "calculate",
    "define",
    "state",
    "explicitly",
    "directly",
    "outlined",
    "reported",
    "reporting",
    "report",
    "fiscal",
    "fy",
    "year",
    "years",
    "usd",
    "million",
    "millions",
    "billion",
    "billions",
    "quantity",
    "amount",
    "total",
    "primarily",
    "important",
    "materially",
    "ongoing",
    "any",
    "not",
    "if",
    "then",
    "than",
    "unadjusted",
    "adjusted",
    "based",
    "rely",
    "relying",
    "within",
    "according",
])
# A small financial vocabulary bridges question wording to ordinary statement headings.
# These are concepts, not company names, question identifiers, or evidence locations.
_CONCEPTS = (
    (
        ("geograph", "countr", "region", "international", "operate"),
        (
            "geographic segments",
            "reportable segments",
            "business divisions",
            "international operations",
            "countries regions",
        ),
    ),
    (("legal", "litigat", "lawsuit", "battle"), ("legal proceedings", "litigation contingencies", "claims lawsuits")),
    (("restructur",), ("restructuring charges", "restructuring costs")),
    (("income statement", "p l", "profit loss"), ("statements income", "statements operations", "net revenue income")),
    (("cash flow",), ("statements cash flows", "operating investing financing")),
    (
        ("ebitda", "d a", "depreciat", "amortiz"),
        ("depreciation amortization", "operating income", "statements cash flows"),
    ),
    (("operating income", "operating profit", "ebitda"), ("operating income", "operating profit")),
    (("capex", "capital expenditur"), ("capital expenditures", "additions property plant equipment")),
    (("balance sheet", "financial position"), ("balance sheets", "assets liabilities equity")),
    (("revenue", "sales"), ("net revenue", "net sales")),
    (("debt", "borrow", "leverage"), ("long term debt", "borrowings interest")),
    (("dividend", "repurchas", "buyback"), ("dividends repurchases", "treasury stock")),
    (("inventory", "inventories"), ("inventories", "inventory")),
    (("tax",), ("income taxes", "effective tax rate")),
    (("pension", "retirement"), ("pension benefits", "retirement plans")),
    (("research", "r d"), ("research development",)),
)


def _terms(text: str) -> tuple[str, ...]:
    # FY2024 and 2024 share the year token; punctuation/underscores do not join words.
    return tuple(word for word in _WORD.findall(text.lower()) if word not in _STOP)


def _stem(word: str) -> str:
    if len(word) > 5 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _query_parts(question: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    original = tuple(dict.fromkeys(_terms(question)))
    normalized = " ".join(_WORD.findall(question.lower()))
    expansions = tuple(
        dict.fromkeys(
            phrase
            for triggers, phrases in _CONCEPTS
            if any(re.search(r"\b" + re.escape(trigger), normalized) for trigger in triggers)
            for phrase in phrases
        )
    )
    return original, expansions


def _rank(hits: list[MemoryHit], original: tuple[str, ...], expansions: tuple[str, ...]) -> list[MemoryHit]:
    documents = [Counter(_stem(term) for term in _terms(hit.text.partition("\n")[2])) for hit in hits]
    frequency: Counter[str] = Counter()
    for document in documents:
        frequency.update(document.keys())
    average_length = sum(sum(document.values()) for document in documents) / max(len(documents), 1)
    weights = dict.fromkeys((_stem(term) for phrase in expansions for term in _terms(phrase)), 0.45)
    weights.update(dict.fromkeys((_stem(term) for term in original), 1.0))
    phrases = [" ".join(_stem(term) for term in _terms(phrase)) for phrase in expansions]

    def score(index: int) -> float:
        hit, document = hits[index], documents[index]
        length = sum(document.values())
        result = 0.0
        for term, weight in weights.items():
            count = document[term]
            inverse_frequency = math.log(1 + (len(hits) - frequency[term] + 0.5) / (frequency[term] + 0.5))
            denominator = count + 1.2 * (0.4 + 0.6 * length / max(average_length, 1))
            result += weight * inverse_frequency * count * 2.2 / denominator
        body = " ".join(_stem(term) for term in _terms(hit.text.partition("\n")[2]))
        heading = body[:350]
        for phrase in phrases:
            if phrase and phrase in body:
                result += 1.5
                if phrase in heading:
                    result += 4.5
        document_terms = set(_terms(hit.text.partition("\n")[0])) - {"10", "k", "q", "annual"}
        # Document names are supplied input, used only as ordinary query evidence.
        # A company/year match helps distinguish otherwise identical financial tables.
        result += 2 * len(document_terms.intersection(original))
        return result

    return [hits[index] for index in sorted(range(len(hits)), key=lambda index: (-score(index), index))]


async def search_pdf_pages(
    memory: ScopedMemoryApplication, question: str, limit: int = 10
) -> tuple[MemoryCitation, ...]:
    """Return distinct real PDF page citations, without leaving ``memory``'s scope.

    Short concept queries avoid the full-text engine's long-question admission
    threshold. The union is reranked locally; only citations verified through
    ``get`` with a single PDF page Source are returned. No parse-time state is used.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit must be a nonnegative integer")  # noqa: TRY003
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must contain non-whitespace text")  # noqa: TRY003
    original, expansions = _query_parts(question)
    if limit == 0 or not original:
        return ()
    queries = tuple(
        dict.fromkeys((
            " ".join(original),
            *expansions,
            *(" ".join(original[index : index + 4]) for index in range(0, len(original), 4)),
        ))
    )
    candidates: dict[tuple[str, str, str], MemoryHit] = {}
    for query in queries:
        page = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=max(96, limit * 4)))
        for hit in page.hits:
            key = (hit.memory_ref.artifact_id, hit.entry_id, hit.entry_version_id)
            candidates.setdefault(key, hit)
    seen: set[str] = set()
    citations: list[MemoryCitation] = []
    for hit in _rank(list(candidates.values()), original, expansions):
        citation = MemoryCitation(
            memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id
        )
        record = await memory.get(GetMemoryEntryRequest(citation=citation))
        sources = record.entry.sources
        if len(sources) != 1 or sources[0].source_type != "pdf-page" or record.state != "active":
            continue
        identity = sources[0].source_id
        if identity in seen:
            continue
        seen.add(identity)
        citations.append(citation)
        if len(citations) == limit:
            break
    return tuple(citations)
