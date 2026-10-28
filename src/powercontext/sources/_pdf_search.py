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
from itertools import pairwise
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
    "explain",
    "useful",
    "metric",
    "like",
    "conventional",
    "meaningful",
    "roughly",
    "many",
    "much",
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
    (
        ("income statement", "p l", "profit loss"),
        ("statements income", "income statements", "statements operations", "net revenue income"),
    ),
    (("cash flow",), ("statements cash flows", "cash flows statements", "operating investing financing")),
    (
        ("ebitda", "d a", "depreciat", "amortiz"),
        ("depreciation amortization", "operating income", "statements cash flows"),
    ),
    (("operating income", "operating profit", "ebitda"), ("operating income", "operating profit")),
    (("capex", "capital expenditur"), ("capital expenditures", "additions property plant equipment")),
    (
        ("capital intens", "asset intens"),
        (
            "capital expenditures",
            "capital spending",
            "property plant equipment",
            "total assets",
            "net sales",
            "net revenue",
            "balance sheets",
            "statements income",
            "income statements",
            "statements operations",
            "statements cash flows",
            "cash flows statements",
        ),
    ),
    (("segment", "business unit"), ("segment results", "segment operating", "sales growth")),
    (
        ("organic", "m a", "excluding acquisit", "exclude acquisit", "without acquisit", "without merger"),
        ("organic sales", "organic growth", "acquisitions divestitures"),
    ),
    (("balance sheet", "financial position"), ("balance sheets", "assets liabilities equity")),
    (("ppne", "pp e", "ppe"), ("property plant equipment", "balance sheets")),
    (("customer",), ("customers", "customer", "major customers", "customer concentration")),
    (("categor", "segment", "business unit"), ("business segment data", "segment information")),
    (("cyclic",), ("cyclical", "cycles", "cyclicality")),
    (("revenue", "sales"), ("net revenue", "net sales")),
    (("debt", "borrow", "leverage"), ("long term debt", "borrowings interest")),
    (("dividend", "repurchas", "buyback"), ("dividends repurchases", "treasury stock")),
    (("inventory", "inventories"), ("inventories", "inventory")),
    (("tax",), ("income taxes", "effective tax rate")),
    (("pension", "retirement"), ("pension benefits", "retirement plans")),
    (("research", "r d"), ("research development",)),
    # Ratios are often calculated from statement rows that never name the ratio.
    # Retrieve those operands as well as discussions mentioning the metric.
    (
        ("gross margin", "gross profit"),
        ("net sales", "net revenue", "cost sales", "cost revenue", "cost goods sold", "statements operations"),
    ),
    (("operating margin",), ("operating income", "operating profit", "net sales", "net revenue")),
    (("net margin", "profit margin"), ("net income", "net earnings", "net sales", "net revenue")),
    (("working capital", "current ratio"), ("current assets", "current liabilities", "balance sheets")),
    (
        ("quick ratio", "acid test"),
        ("cash equivalents", "short term investments", "accounts receivable", "current liabilities", "balance sheets"),
    ),
    (
        ("return on assets", "asset return"),
        (
            "net income",
            "net earnings",
            "total assets",
            "statements operations",
            "statements income",
            "income statements",
            "balance sheets",
        ),
    ),
    (
        ("return on equity", "equity return"),
        ("net income", "net earnings", "shareholders equity", "stockholders equity", "balance sheets"),
    ),
    (
        ("debt to equity", "debt equity", "financial leverage"),
        ("total debt", "total liabilities", "shareholders equity", "stockholders equity", "balance sheets"),
    ),
    (("asset turnover",), ("net sales", "net revenue", "total assets", "balance sheets")),
    (
        ("inventory turnover", "inventory days", "days inventory"),
        ("inventories", "cost sales", "cost revenue", "cost goods sold", "balance sheets"),
    ),
    (
        ("cash conversion cycle", "cash operating cycle"),
        (
            "trade receivables",
            "accounts receivable",
            "inventories",
            "trade payables",
            "accounts payable",
            "cost sales",
            "cost revenue",
            "net sales",
            "net revenue",
            "balance sheets",
            "statements operations",
        ),
    ),
    (
        ("free cash flow",),
        ("operating activities", "capital expenditures", "capital spending", "statements cash flows"),
    ),
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
    # Preserve sequence for names with repeated tokens and names preceded by an
    # acronym sharing one of their tokens. Deduplication belongs to query weights.
    original = _terms(question)
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


def _document_words(document_id: str) -> tuple[str, ...]:
    return tuple(term for term in _terms(document_id) if term not in {"10", "k", "q", "annual", "report", "form"})


def _named_documents(hits: list[MemoryHit], original: tuple[str, ...]) -> set[str]:
    query_names = {
        "".join(original[start:end]) for start in range(len(original)) for end in range(start + 1, len(original) + 1)
    }
    patterns: dict[str, re.Pattern[str]] = {}
    matched: set[str] = set()
    for hit in hits:
        document_id, _, body = hit.text.partition("\n")
        name = "".join(term for term in _document_words(document_id) if not (term.isdigit() and len(term) == 4))
        if not name:
            continue
        if name in query_names:
            matched.add(document_id)
            continue
        if document_id not in patterns:
            # A declaration must name this issuer; a competitor's alias is not
            # evidence about the owning document. Spacing in IDs is optional.
            patterns[document_id] = re.compile(
                r"\b"
                + r"[\W_]*".join(map(re.escape, name))
                + r"\b(?:\s+(?:Company|Corporation|Inc\.?|Co\.?|Limited|Ltd\.?|PLC))*"
                + r"\s*\(\s*[“\"']?([a-zA-Z][a-zA-Z0-9&. -]{1,30})[”\"']?\s*\)",
                re.IGNORECASE,
            )
        aliases = {"".join(_terms(match.group(1))) for match in patterns[document_id].finditer(body)}
        aliases.update(
            match.group(1).lower().rstrip(".")
            for match in re.finditer(
                r"\bThe Company[’']s (?:Common Stock|shares)\b[^.\n]{0,180}"  # noqa: RUF001 - PDF smart quotes.
                r"\bsymbol\s+[“\"']?([A-Z][A-Z0-9.]{0,8})\b",
                body,
            )
        )
        if (aliases - {"company", "corporation", "inc", "co", "group", "us", "registrant"}) & query_names:
            matched.add(document_id)
    return matched


def _statement_title(text: str) -> bool:
    lines = text.splitlines()[:12]
    titles = [*lines, *(f"{left} {right}" for left, right in pairwise(lines))]
    # Match a complete title, including a wrapped title, rather than an audit
    # paragraph or note that merely refers to the consolidated statements.
    pattern = (
        r"(?:(?:consolidated|combined|condensed) )+"
        r"(?:statement (?:income|earning|operation|cash flow|financial position)"
        r"|(?:income|earning|cash flow) statement|balance sheet)"
    )
    return any(re.fullmatch(pattern, " ".join(_stem(term) for term in _terms(title))) for title in titles)


def _rank(hits: list[MemoryHit], original: tuple[str, ...], expansions: tuple[str, ...]) -> list[MemoryHit]:
    document_words = [_document_words(hit.text.partition("\n")[0]) for hit in hits]
    matched_documents = _named_documents(hits, original)
    named_documents = [hit.text.partition("\n")[0] in matched_documents for hit in hits]
    documents = [Counter(_stem(term) for term in _terms(hit.text.partition("\n")[2])) for hit in hits]
    frequency: Counter[str] = Counter()
    for document in documents:
        frequency.update(document.keys())
    average_length = sum(sum(document.values()) for document in documents) / max(len(documents), 1)
    weights = dict.fromkeys((_stem(term) for phrase in expansions for term in _terms(phrase)), 0.45)
    names = {term for words, matched in zip(document_words, named_documents, strict=True) if matched for term in words}
    content_terms = tuple(_stem(term) for term in original if not term.isdigit() and term not in names)
    weights.update(dict.fromkeys(content_terms, 1.0))
    # Singular/plural aliases must not multiply the same phrase's ranking bonus.
    phrases = tuple(dict.fromkeys(" ".join(_stem(term) for term in _terms(phrase)) for phrase in expansions))
    financial = bool(
        set(content_terms)
        & {
            "margin",
            "capital",
            "asset",
            "income",
            "earning",
            "revenue",
            "sale",
            "cash",
            "tax",
            "ppne",
            "ppe",
            "ebitda",
        }
    )
    statements = [
        (financial or "inventory" in content_terms) and named and _statement_title(hit.text.partition("\n")[2])
        for hit, named in zip(hits, named_documents, strict=True)
    ]
    customers = "customer" in content_terms
    revenue_sources = [
        customers
        and named
        and bool(
            re.search(
                r"\b(?:revenues?|sales)\b[^.]{0,100}\b(?:from|pursuant to|attributable to)\b",
                " ".join(hit.text.partition("\n")[2].lower().split()),
            )
        )
        for hit, named in zip(hits, named_documents, strict=True)
    ]

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
        # Titles that use the question's own nouns are as useful as titles from
        # the financial vocabulary. This also handles novel topic wording.
        result += 3 * sum(term in heading.split() for term in set(content_terms))
        if financial and re.search(
            r"\b(?:statement (?:income|earning|operation|cash flow|financial position)"
            r"|(?:income|earning|cash flow) statement|balance sheet)\b",
            heading,
        ):
            # Quantitative questions often require operands on several core
            # statements, including when their requested ratio is not reported.
            result += 12
        for left, right in set(pairwise(content_terms)):
            if f" {left} {right} " in f" {body} ":
                result += 2
        # Document names are supplied input, used only as ordinary query evidence.
        # A company/year match helps distinguish otherwise identical financial tables.
        result += 2 * len(set(document_words[index]).intersection(original))
        return result

    return [
        hits[index]
        for index in sorted(
            range(len(hits)),
            key=lambda index: (
                not named_documents[index],
                not revenue_sources[index],
                not statements[index],
                -score(index),
                index,
            ),
        )
    ]


async def search_pdf_pages(  # noqa: C901 - Search, document expansion, and exact citation validation.
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
    search_terms = tuple(dict.fromkeys(original))
    abbreviations = tuple(
        word.lower()
        for word in re.findall(r"\b[A-Za-z][A-Za-z0-9&]*\b", question)
        if sum(character.isupper() for character in word) >= 2
    )
    queries = tuple(
        dict.fromkeys((
            " ".join(search_terms),
            *expansions,
            *abbreviations,
            *(" ".join(search_terms[index : index + 4]) for index in range(0, len(search_terms), 4)),
        ))
    )
    candidates: dict[tuple[str, str, str], MemoryHit] = {}
    for query in queries:
        page = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=max(96, limit * 4)))
        for hit in page.hits:
            key = (hit.memory_ref.artifact_id, hit.entry_id, hit.entry_version_id)
            candidates.setdefault(key, hit)
    # Scope stays with the caller's public API. A document explicitly identified
    # by the question can supply tables that lack the question's surface words.
    for document_id in sorted(_named_documents(list(candidates.values()), original)):
        page = await memory.search(SearchMemoryRequest(query=document_id, mode="fts", limit=max(1000, limit * 4)))
        for hit in page.hits:
            if hit.text.partition("\n")[0] == document_id:
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
