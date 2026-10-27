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

"""Query-directed, offline selection of literal, precisely addressed Memory spans."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from powercontext.artifacts import ArtifactAddress
from powercontext.builtin.runtime.prepared_text import TRUST_POLICY

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_BOUNDARY = re.compile(r"[.!?]+[\"'”’)]*(?=\s|$)|[。！？]+[\"'”’)]*|\n+")  # noqa: RUF001
_STOP_WORDS = frozenset([
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "been",
    "being",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "for",
    "from",
    "had",
    "has",
    "have",
    "how",
    "in",
    "into",
    "is",
    "it",
    "its",
    "may",
    "of",
    "on",
    "or",
    "our",
    "should",
    "that",
    "the",
    "their",
    "these",
    "this",
    "those",
    "to",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "will",
    "with",
    "would",
    "we",
    "you",
    "your",
])


@dataclass(frozen=True)
class MemoryEntryAddress:
    """Identify one exact Memory entry version across Scope boundaries."""

    memory: ArtifactAddress
    entry_id: str
    entry_version_id: str


@dataclass(frozen=True)
class PreparedMemoryFragment:
    """A half-open Python character interval copied without altering its source."""

    address: MemoryEntryAddress
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class _Span:
    document: int
    start: int
    end: int
    score: float


def _tokens(text: str) -> list[str]:
    words = []
    for match in _WORD.finditer(text):
        word = match.group().casefold()
        if word in _STOP_WORDS:
            continue
        # Normalize common inflections without a model or a language-data download.
        for suffix in ("ions", "ion", "ing", "ed", "es", "s"):
            if len(word) > len(suffix) + 3 and word.endswith(suffix) and not word.endswith("ss"):
                word = word[: -len(suffix)]
                break
        if len(word) > 4 and word.endswith("e"):
            word = word[:-1]
        words.append(word)
    return words


def _sentences(text: str) -> list[tuple[int, int]]:
    ranges = []
    start = 0
    for boundary in _BOUNDARY.finditer(text):
        ranges.append((start, boundary.end()))
        start = boundary.end()
    ranges.append((start, len(text)))
    expanded = []
    for start, end in ranges:
        if not text[start:end].strip():
            continue
        # Whitespace belongs to the original evidence too. Include it on either
        # side; neighbouring ranges will merge before rendering and charging bytes.
        while start > 0 and text[start - 1].isspace():
            start -= 1
        while end < len(text) and text[end].isspace():
            end += 1
        expanded.append((start, end))
    return expanded


def _rank(query: str, texts: Sequence[str]) -> list[_Span]:
    query_words = set(_tokens(query)) - _STOP_WORDS
    if not query_words:
        query_words = set(_tokens(query))
    document_words = [set(_tokens(text)) for text in texts]
    weights = {
        word: 1.0 + math.log((len(texts) + 1) / (1 + sum(word in words for words in document_words)))
        for word in query_words
    }
    total = sum(weights.values()) or 1.0
    spans = []
    for document, text in enumerate(texts):
        document_match = sum(weights[word] for word in query_words & document_words[document]) / total
        for start, end in _sentences(text):
            words = _tokens(text[start:end])
            overlap = sum(weights[word] for word in query_words & set(words)) / total
            # Coverage does not reward repeated query words. The packing step charges
            # actual bytes, so evidence is not penalized a second time for its length.
            score = (overlap + 0.05 * document_match) * (1.0 + document_match)
            spans.append(_Span(document, start, end, score))
    return sorted(spans, key=lambda span: (-span.score, span.document, span.start))


def _merge(spans: Sequence[_Span], texts: Sequence[str]) -> list[_Span]:
    merged: list[_Span] = []
    for span in sorted(spans, key=lambda item: (item.document, item.start, item.end)):
        if merged:
            previous = merged[-1]
            if previous.document == span.document and not texts[span.document][previous.end : span.start].strip():
                merged[-1] = _Span(
                    span.document, previous.start, max(previous.end, span.end), previous.score + span.score
                )
                continue
        merged.append(span)
    return sorted(merged, key=lambda span: (-span.score, span.document, span.start))


def _fragments(
    spans: Sequence[_Span], documents: Sequence[tuple[MemoryEntryAddress, str]]
) -> tuple[PreparedMemoryFragment, ...]:
    return tuple(
        PreparedMemoryFragment(
            documents[span.document][0], span.start, span.end, documents[span.document][1][span.start : span.end]
        )
        for span in spans
    )


def _render(fragments: Sequence[PreparedMemoryFragment]) -> str:
    items = [
        {
            "citation": {
                "scope_id": fragment.address.memory.scope_id,
                "memory_ref": fragment.address.memory.artifact.model_dump(mode="json"),
                "entry_id": fragment.address.entry_id,
                "entry_version_id": fragment.address.entry_version_id,
            },
            "content": fragment.text,
            "start": fragment.start,
            "end": fragment.end,
        }
        for fragment in fragments
    ]
    encoded = json.dumps({"trust": "untrusted_history", "items": items}, ensure_ascii=False, separators=(",", ":"))
    return f"{TRUST_POLICY}\n\nBEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1\n{encoded}\nEND_POWERCONTEXT_PREPARED_CONTEXT_V1"


def select_evidence(
    query: str, documents: Sequence[tuple[MemoryEntryAddress, str]], max_bytes: int
) -> tuple[str | None, tuple[PreparedMemoryFragment, ...]]:
    """Select whole sentences, merging neighbours to amortize exact citation costs.

    Every fit is measured using the actual JSON and envelope, including Unicode and
    escaping. Only when no complete sentence fits do we consider a literal query window.
    """
    texts = [text for _, text in documents]
    remaining = _rank(query, texts)
    selected: list[_Span] = []
    content: str | None = None
    fragments: tuple[PreparedMemoryFragment, ...] = ()
    while remaining:
        best = None
        for index, span in enumerate(remaining):
            candidate = _merge((*selected, span), texts)
            candidate_fragments = _fragments(candidate, documents)
            rendered = _render(candidate_fragments)
            cost = len(rendered.encode("utf-8"))
            if cost > max_bytes:
                continue
            # Adjacent evidence can reuse a citation. Select by marginal relevance per
            # byte, with a sublinear cost penalty so tiny sentences do not dominate.
            added = max(1, cost - (len(content.encode("utf-8")) if content is not None else 0))
            value = (span.score + 0.001) / added**0.35
            if best is None or value > best[0]:
                best = (value, index, candidate, candidate_fragments, rendered)
        if best is None:
            break
        _, index, selected, fragments, content = best
        remaining.pop(index)
    if fragments:
        return content, fragments
    return _fit_window(query, remaining, documents, max_bytes)


def _fit_window(
    query: str, spans: Sequence[_Span], documents: Sequence[tuple[MemoryEntryAddress, str]], max_bytes: int
) -> tuple[str | None, tuple[PreparedMemoryFragment, ...]]:
    query_words = set(_tokens(query)) - _STOP_WORDS
    for span in spans:
        text = documents[span.document][1]
        matches = [
            match
            for match in _WORD.finditer(text, span.start, span.end)
            if query_words.intersection(_tokens(match.group()))
        ]
        center = matches[0].start() if matches else span.start
        lower, upper = 1, span.end - span.start
        best: tuple[str | None, tuple[PreparedMemoryFragment, ...]] = (None, ())
        while lower <= upper:
            width = (lower + upper) // 2
            start = max(span.start, min(center - width // 3, span.end - width))
            candidate = _Span(span.document, start, start + width, span.score)
            fragments = _fragments((candidate,), documents)
            content = _render(fragments)
            if len(content.encode("utf-8")) <= max_bytes:
                best = (content, fragments)
                lower = width + 1
            else:
                upper = width - 1
        if best[1]:
            return best
    return None, ()
