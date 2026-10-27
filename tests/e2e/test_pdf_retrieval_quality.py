"""Synthetic, independently labelled retrieval groups; not FinanceBench metrics."""

from __future__ import annotations

import asyncio
import json
from typing import TypedDict

from powercontext.sources.pdf_pages import PDFDocumentInput, parse_pdf_document
from tests.e2e.test_pdf_ingestion import _inspect, _load
from tests.pdf_test_support import pdf_bytes, terms, text_metrics


class _QueryMetric(TypedDict):
    question: str
    hits: int
    evidence_pages: int
    missed: list[tuple[str, int]]


class _GroupMetric(TypedDict):
    group: str
    pages: int
    questions: int
    hits: int
    evidence_pages: int
    recall_at_20: float
    queries: list[_QueryMetric]


_DISTRACTORS = (
    "Corporate governance. Directors approved committee appointments and changes in the articles of association.",
    "Inventory logistics. Distribution centers handle raw materials and finished goods. Stock movements depend on seasonal demand.",
    "Income tax jurisdictions. Subsidiaries file returns in local countries. Administrative deadlines differ across jurisdictions.",
    "Debt risk. The credit rating can affect access to funding. Management monitors bank counterparty credit exposures.",
    "Research collaboration. Universities provide laboratory access. Development teams hold regular innovation workshops.",
    "Pension administration. Employees receive annual benefit statements. Retirement plan trustees meet every quarter.",
    "Lease policies. Warehouses are reviewed for suitability. A property committee approves new rental contracts.",
    "Revenue recognition. Sales are recorded when control transfers. Customer rebates reduce the transaction price.",
    "Foreign currency translation. Average exchange rates translate sales and expenses. Closing rates translate balance sheet accounts.",
    "Share capital. Ordinary shareholders may vote at the general meeting. No preferred shares are outstanding.",
    "Tax risk. New legislation could change taxation. This discussion does not estimate the effective income tax rate.",
    "Borrowing covenants. Financing agreements contain customary restrictions on asset sales and additional indebtedness.",
    "Research staffing. Engineers receive technical training. Laboratory hiring supports the product development strategy.",
    "Inventory risk. Supply interruptions may affect goods held for resale. Manufacturing plants maintain contingency plans.",
    "Retirement workforce. Succession planning addresses demographic changes. Employees can attend retirement education seminars.",
    "Operating lease facilities. Corporate offices are located near transport links. No property acquisitions are currently planned.",
    "Auditor opinion. The independent auditor reviewed the financial statements and related disclosures in all material respects.",
    "Capital investment. The board evaluates projects using discounted cash flows. Spending approvals consider available liquidity.",
    "Legal proceedings. Management does not expect pending claims to materially affect consolidated financial condition.",
    "Accounting estimates. Management evaluates assumptions on an ongoing basis. Actual results may differ from estimates.",
)

_EVIDENCE = {
    "inventory_method": "Inventories accounting policy. Finished goods are valued using the first-in first-out method. Raw materials use weighted average cost. Inventories are carried at the lower of cost and net realizable value.",
    "inventory_balance": "Inventories balance at year end. Raw materials 345. Work in process 210. Finished goods 789. Total inventories 1344. Prior year total 1290. Amounts in millions.",
    "tax_expense": "Provision for income taxes. Current tax expense 205. Deferred tax expense 38. Total income tax expense 243. Prior year income tax expense 220. Amounts in millions.",
    "pretax_income": "Consolidated statements of operations. Net sales 8950. Operating profit 1520. Interest expense 170. Income before income taxes 1350. Net earnings 1107. Amounts in millions.",
    "debt": "Borrowings and contractual debt maturities. Scheduled principal repayments: next year 610; second year 420; third year 355; thereafter 1800. Long-term debt outstanding 3185. Amounts in millions.",
    "research": "Research and development expenses. Product research expenditure is recognized as incurred. Research and development expense for the current year was 286 compared with 249 in the prior year. Amounts in millions.",
    "pension_obligation": "Defined benefit pension obligations. Projected benefit obligation at year end 2450. Pension service cost 95. Interest cost 81. Accumulated benefit obligation 2290. Amounts in millions.",
    "pension_assets": "Retirement plans funded status. Fair value of pension plan assets at year end 2120. Employer contributions 135. Benefits paid 155. Assets comprise equities and fixed-income securities. Amounts in millions.",
    "lease_payments": "Operating lease commitments. Future minimum rental payments are 146 next year, 132 in the second year and 99 in the third year. Aggregate undiscounted payments 377. Amounts in millions.",
    "lease_discount": "Leases measurement. The weighted average discount rate for operating lease liabilities is 4.7 percent. The weighted average remaining operating lease term is 5.2 years. Finance lease discount rate is 5.1 percent.",
}


def _quality_report(company, year, reverse):
    contents = [
        *_DISTRACTORS,
        *_EVIDENCE.values(),
        "Business overview. Manufacture and distribution of industrial products.",
        "Dividend policy. The board reviews distributions after considering cash requirements.",
    ]
    # Report layouts differ across companies without changing the known evidence text.
    if reverse:
        contents.reverse()
    texts = tuple(f"{company} Annual Financial Report {year}\n{text}" for text in contents)
    document_id = f"{company.upper()}_{year}_REPORT"
    pages = parse_pdf_document(PDFDocumentInput(document_id=document_id, content=pdf_bytes(texts)))
    positions = {topic: (document_id, contents.index(text) + 1) for topic, text in _EVIDENCE.items()}
    token_ratios = [
        sum(terms(page.text).values()) / sum(terms(text).values()) for page, text in zip(pages, texts, strict=True)
    ]
    page_f1 = [text_metrics(page.text, text)[1] for page, text in zip(pages, texts, strict=True)]
    evidence_recall = [text_metrics(pages[positions[topic][1] - 1].text, text)[0] for topic, text in _EVIDENCE.items()]
    assert all(ratio == 1.0 for ratio in token_ratios)
    assert min(page_f1) == min(evidence_recall) == 1.0
    fidelity = {
        "page_token_ratio_min": min(token_ratios),
        "page_token_ratio_max": max(token_ratios),
        "page_token_f1_min": min(page_f1),
        "evidence_token_recall_min": min(evidence_recall),
    }
    return pages, positions, fidelity


def _questions(first, second, year, first_positions, second_positions):
    return [
        (
            f"Which inventory valuation method did {first} use in {year}, and what was its year-end finished goods balance?",
            {first_positions["inventory_method"], first_positions["inventory_balance"]},
        ),
        (
            f"For {second} in {year}, what income tax expense and earnings before tax should be used to determine its effective tax rate?",
            {second_positions["tax_expense"], second_positions["pretax_income"]},
        ),
        (
            f"How much of {first}'s borrowings must be repaid in the next year according to its {year} maturity schedule?",
            {first_positions["debt"]},
        ),
        (
            f"How much did {second} expense on R&D during {year}, compared with the preceding year?",
            {second_positions["research"]},
        ),
        (
            f"What pension obligation and plan asset values determine {first}'s retirement funding shortfall at the end of {year}?",
            {first_positions["pension_obligation"], first_positions["pension_assets"]},
        ),
        (
            f"For {second} in {year}, what operating lease payments are due next year and what discount rate measures the lease liabilities?",
            {second_positions["lease_payments"], second_positions["lease_discount"]},
        ),
    ]


def test_synthetic_groups_recall_twenty_with_financial_distractors(tmp_path):
    groups = []
    fidelity = []
    for group, first, second, year in (
        ("industrial", "Cedar", "Elm", 2034),
        ("manufacturing", "Juniper", "Maple", 2027),
    ):
        first_pages, first_positions, first_fidelity = _quality_report(first, year, reverse=False)
        second_pages, second_positions, second_fidelity = _quality_report(second, year, reverse=True)
        fidelity.extend([first_fidelity, second_fidelity])
        groups.append((
            group,
            first_pages + second_pages,
            _questions(first, second, year, first_positions, second_positions),
        ))
    database = tmp_path / "quality.db"
    scopes = asyncio.run(_load(database, tuple(pages for _, pages, _ in groups)))
    metrics: list[_GroupMetric] = []
    for scope, (group, pages, questions) in zip(scopes, groups, strict=True):
        observed = asyncio.run(_inspect(database, scope, [[question, 20] for question, _ in questions]))
        assert len(observed["pages"]) == len(pages) == 64
        query_metrics: list[_QueryMetric] = []
        for (question, gold), results in zip(questions, observed["results"], strict=True):
            actual = {(document_id, page_number) for document_id, _, page_number in results}
            query_metrics.append({
                "question": question,
                "hits": len(actual & gold),
                "evidence_pages": len(gold),
                "missed": sorted(gold - actual),
            })
        hits = sum(query["hits"] for query in query_metrics)
        total = sum(query["evidence_pages"] for query in query_metrics)
        metrics.append({
            "group": group,
            "pages": len(pages),
            "questions": len(questions),
            "hits": hits,
            "evidence_pages": total,
            "recall_at_20": hits / total,
            "queries": query_metrics,
        })
    print(
        json.dumps({
            "dataset": "self_constructed_not_FinanceBench",
            "physical_pages": 128,
            "extraction_fidelity": fidelity,
            "groups": metrics,
        })
    )
    assert all(group["recall_at_20"] >= 0.70 for group in metrics), metrics
