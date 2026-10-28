"""Offline public-label scoring; never imported by the PDF product or retrieval process.

Run after tests.validate_pdf_public, passing the explicitly obtained public JSONL,
expected.json and restart.json. Also accepts the independent search-result format
with ``questions: [{id, locations}]``. No answers or justifications are consumed.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from tests.pdf_test_support import text_metrics


def score(labels: Path, expected: Path, actual: Path) -> dict[str, object]:
    annotations = {row["financebench_id"]: row for row in map(json.loads, labels.read_text().splitlines())}
    request, response = json.loads(expected.read_text()), json.loads(actual.read_text())
    pages = {(page["document_id"], page["page_number"]): page for page in request["pages"]}
    assert len(pages) == len(request["pages"]), "label comparison requires one revision per document"
    groups: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    results = []
    all_fidelity = []
    for question, returned in zip(request["questions"], response["questions"], strict=True):
        question_id = question["financebench_id"]
        assert returned.get("id", question_id) == question_id
        assert returned.get("question", question["question"]) == question["question"]
        label = annotations[question_id]
        locations = returned["locations"]
        assert len(locations) <= 20
        assert len({tuple(location) for location in locations}) == len(locations)
        for document, digest, page in locations:
            assert pages[(document, page)]["document_sha256"] == digest
        actual_pages = {(document, page) for document, _, page in locations}
        gold = {(evidence["doc_name"], evidence["evidence_page_num"] + 1) for evidence in label["evidence"]}
        hit_count = len(actual_pages & gold)
        groups[label["doc_name"]][0] += hit_count
        groups[label["doc_name"]][1] += len(gold)
        fidelity = []
        for evidence in label["evidence"]:
            page = pages[(evidence["doc_name"], evidence["evidence_page_num"] + 1)]
            recall, _ = text_metrics(page["text"], evidence["evidence_text"])
            _, f1 = text_metrics(page["text"], evidence["evidence_text_full_page"])
            fidelity.append({"evidence_recall": recall, "page_f1": f1})
        results.append({"id": question_id, "hits": hit_count, "total": len(gold), "fidelity": fidelity})
        all_fidelity.extend(fidelity)
    hits, total = sum(value[0] for value in groups.values()), sum(value[1] for value in groups.values())
    return {
        "questions": len(results),
        "hits": hits,
        "total": total,
        "recall_at20": hits / total if total else 1.0,
        "documents": {
            document: {"hits": found, "total": count, "recall_at20": found / count}
            for document, (found, count) in sorted(groups.items())
        },
        "min_evidence_recall": min(item["evidence_recall"] for item in all_fidelity),
        "min_page_f1": min(item["page_f1"] for item in all_fidelity),
        "all_document_groups_pass": all(found / count >= 0.70 for found, count in groups.values()),
        "all_text_quality_pass": all(min(item.values()) >= 0.95 for item in all_fidelity),
        "frozen_page_term_counts": "unavailable; not evaluated",
        "results": results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("labels", type=Path)
    parser.add_argument("expected", type=Path)
    parser.add_argument("actual", type=Path)
    args = parser.parse_args()
    result = score(args.labels, args.expected, args.actual)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    raise SystemExit(0 if result["all_document_groups_pass"] and result["all_text_quality_pass"] else 1)
