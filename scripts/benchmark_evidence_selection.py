"""Measure literal selection on explicitly supplied SciFact files, outside the product.

This is a local benchmark, not the competition's unpublished 24-claim workload.
Candidates contain one annotated document and its two strongest lexical distractors.
Only claims whose complete annotated evidence fits 1,400 bytes are measured, at both
1,400 and 1,800 bytes. Product calls receive no annotations or document IDs.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import statistics
from collections import Counter
from pathlib import Path

from powercontext.builtin.artifacts.memory import MemoryEntryInput, MemoryHit
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CaptureSource,
    GetMemoryEntryRequest,
    PrepareContextRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.prepared_context import PreparedContextBuilder, PreparedMemoryCandidates
from powercontext.builtin.runtime.prepared_text import TRUST_POLICY
from powercontext.builtin.scope import ScopeDraft
from powercontext.builtin.sources import ContentSource

_REPOSITORY = Path(__file__).resolve().parents[1]


class LiteralPipeline:
    async def extract(self, request):
        return tuple(
            MemoryEntryInput(kind="document", text=source.content, sources=(source,))
            for source in request.sources
            if isinstance(source, ContentSource)
        )


def tokens(text):
    return re.findall(r"[a-z]+|\d+", text.lower())


def ranges(document, indices):
    offset = len(document["title"]) + 1
    result = []
    for index, sentence in enumerate(document["abstract"]):
        if index in indices:
            if result and result[-1][1] + 1 == offset:
                result[-1][1] = offset + len(sentence)
            else:
                result.append([offset, offset + len(sentence)])
        offset += len(sentence) + 1
    return result


def oracle_bytes(scope_id, record, text, intervals):
    items = [
        {
            "citation": {"scope_id": scope_id, **record.citation.model_dump(mode="json")},
            "content": text[start:end],
            "start": start,
            "end": end,
        }
        for start, end in intervals
    ]
    encoded = json.dumps({"trust": "untrusted_history", "items": items}, ensure_ascii=False, separators=(",", ":"))
    return len(
        f"{TRUST_POLICY}\n\nBEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1\n{encoded}\nEND_POWERCONTEXT_PREPARED_CONTEXT_V1".encode()
    )


async def run(args):
    corpus = {str(document["doc_id"]): document for document in map(json.loads, args.corpus.read_text().splitlines())}
    texts = {key: document["title"] + " " + " ".join(document["abstract"]) for key, document in corpus.items()}
    words = {key: Counter(tokens(text)) for key, text in texts.items()}
    frequency = Counter(word for counts in words.values() for word in counts)
    claims = sorted(map(json.loads, args.claims.read_text().splitlines()), key=lambda claim: claim["id"])
    product_sources = {
        str(path.relative_to(_REPOSITORY)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (_REPOSITORY / "src/powercontext/builtin/runtime").glob("*.py")
    }
    rows = []
    examined = skipped = 0
    async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=LiteralPipeline()) as runtime:
        assert runtime.scopes is not None  # noqa: S101
        for claim in claims:
            if len(claim["evidence"]) != 1:
                continue
            if args.limit and examined >= args.limit:
                break
            examined += 1
            gold_id = next(iter(claim["evidence"]))
            query_terms = set(tokens(claim["claim"]))

            def retrieval_score(key, query_terms=query_terms):
                counts = words[key]
                return sum(
                    math.log(1 + len(corpus) / (1 + frequency[word])) * counts[word] / (counts[word] + 1.2)
                    for word in query_terms & counts.keys()
                ) / (0.5 + len(tokens(texts[key])) / 500)

            distractors = sorted(
                (key for key in corpus if key != gold_id), key=lambda key: (-retrieval_score(key), int(key))
            )[:2]
            ids = sorted([gold_id, *distractors], key=lambda key: (-retrieval_score(key), int(key)))
            scope = await runtime.scopes.create(
                ScopeDraft(
                    title="SciFact evidence benchmark",
                    summary="Original documents",
                    idempotency_key=f"claim-{claim['id']}",
                )
            )
            memory = runtime.memory.for_scope(scope.scope_id)
            for document_id in ids:
                await runtime.sources.for_scope(scope.scope_id).capture(
                    CaptureSource(source_id=f"document-{document_id}", content=texts[document_id], metadata={})
                )
            while (await memory.flush()).processed:
                pass
            page = await memory.list()
            assert page.memory_ref is not None  # noqa: S101
            by_text = {record.entry.text: record for record in page.entries}
            gold_record = by_text[texts[gold_id]]
            intervals = ranges(
                corpus[gold_id], {index for evidence in claim["evidence"][gold_id] for index in evidence["sentences"]}
            )
            minimum = oracle_bytes(scope.scope_id, gold_record, texts[gold_id], intervals)
            if minimum > 1400:
                skipped += 1
                continue
            hits = tuple(
                MemoryHit(
                    memory_ref=page.memory_ref,
                    entry_id=by_text[texts[key]].entry.entry_id,
                    entry_version_id=by_text[texts[key]].entry.entry_version_id,
                    text=texts[key],
                    score=retrieval_score(key),
                    matched_by=("fts",),
                )
                for key in ids
            )
            group = PreparedMemoryCandidates(scope_id=scope.scope_id, memory_ref=page.memory_ref, hits=hits)
            for budget in (1400, 1800):
                result = PreparedContextBuilder().build_scopes_result(
                    request=PrepareContextRequest(query=claim["claim"], max_bytes=budget, evidence_selection=True),
                    current_scope_id=scope.scope_id,
                    memory_candidates=(group,),
                )
                selected = [
                    fragment for fragment in result.fragments if fragment.address.entry_id == gold_record.entry.entry_id
                ]
                expected = Counter(tokens(" ".join(texts[gold_id][start:end] for start, end in intervals)))
                actual = Counter(tokens(" ".join(fragment.text for fragment in selected)))
                recall = sum((actual & expected).values()) / sum(expected.values())
                complete = all(
                    any(fragment.start <= start and fragment.end >= end for fragment in selected)
                    for start, end in intervals
                )
                for fragment in result.fragments:
                    record = next(
                        record for record in page.entries if record.entry.entry_id == fragment.address.entry_id
                    )
                    exact = await memory.get(GetMemoryEntryRequest(citation=record.citation))
                    assert fragment.text == exact.entry.text[fragment.start : fragment.end]  # noqa: S101
                assert result.context.content_bytes == len((result.context.content or "").encode()) <= budget  # noqa: S101
                rows.append({
                    "claim_id": claim["id"],
                    "query": claim["claim"],
                    "document_ids": ids,
                    "gold_id": gold_id,
                    "max_bytes": budget,
                    "content_bytes": result.context.content_bytes,
                    "oracle_bytes": minimum,
                    "token_recall": recall,
                    "complete": complete,
                    "gold_intervals": intervals,
                    "selected_gold_intervals": [[fragment.start, fragment.end] for fragment in selected],
                    "fragment_count": len(result.fragments),
                })
    return {
        "workload": "local SciFact single-evidence-document claims with two lexical distractors; all annotation groups unioned; independently feasible at 1400 bytes",
        "official_hidden_workload": False,
        "product_sources_sha256": product_sources,
        "sources": {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (args.corpus, args.claims)},
        "examined_single_document_claims": examined,
        "excluded_infeasible_claims": skipped,
        "requests": len(rows),
        "average_evidence_token_recall": statistics.mean(row["token_recall"] for row in rows) if rows else None,
        "complete_evidence_fraction": statistics.mean(row["complete"] for row in rows) if rows else None,
        "rows": rows,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--claims", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0, help="Maximum single-document claims examined; 0 means all")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    report = asyncio.run(run(arguments))
    arguments.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "rows"}, indent=2))
