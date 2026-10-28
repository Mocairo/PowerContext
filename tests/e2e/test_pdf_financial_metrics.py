"""Financial metric operands must outrank discussions that merely name the metric."""

from __future__ import annotations

import asyncio
import json

from powercontext.sources.pdf_pages import PDFDocumentInput, parse_pdf_document
from tests.e2e.test_pdf_ingestion import _inspect, _load
from tests.pdf_test_support import pdf_bytes


def _financial_report(company: str, year: int, *, reverse: bool):
    discussions = (
        "Gross margin risk. Product profit margins may decline when competitors reduce prices. "
        "No operating results are presented in this risk discussion.",
        "Working capital policy. Management monitors working capital efficiency and funding requirements. "
        "No balance sheet figures are presented in this policy discussion.",
        "Return on assets policy. The board reviews asset investment proposals and expected returns. "
        "Future outcomes can differ from forecasts.",
        "Cash conversion cycle risk. Supplier terms and inventory planning affect liquidity. "
        "No financial statement figures are presented in this risk disclosure.",
    )
    contents = [f"Risk factors section {i + 1}\n{discussions[i % 4]}" for i in range(117)]
    statements = {
        "income": (
            "Consolidated Statements of Operations\nNet sales 9850\nCost of sales 6210\n"
            "Operating profit 1630\nIncome before taxes 1450\nNet earnings 1107"
            if not reverse
            else "INCOME STATEMENTS\nNet revenue 12630\nCost of revenues 7105\n"
            "Income from operations 2010\nIncome before taxes 1750\nNet income 1325"
        ),
        "balance": (
            "Consolidated Balance Sheets\nCash and equivalents 120\nTrade receivables 450\n"
            "Accounts receivable 450\nInventories 680\nTotal current assets 1250\nTotal assets 4500\n"
            "Trade payables 310\nTotal current liabilities 720\nTotal liabilities 2100\nTotal debt 1750\n"
            "Shareholders equity 2400"
        ),
        "cash": (
            ("CASH FLOWS STATEMENTS\n" if reverse else "Consolidated Statement of Cash Flows\n")
            + "Net cash from operating activities 1340\n"
            "Depreciation and amortization 225\nCapital spending (305)\nNet cash used in investing activities (410)"
        ),
    }
    contents.extend(statements.values())
    if reverse:
        contents.reverse()
    document_id = f"{company.upper()}_{year}_10K"
    pages = parse_pdf_document(
        PDFDocumentInput(
            document_id=document_id,
            content=pdf_bytes(tuple(f"{company} Annual Report {year}\n{text}" for text in contents)),
        )
    )
    locations = {kind: (document_id, contents.index(text) + 1) for kind, text in statements.items()}
    return pages, locations


def test_financial_metric_operands_survive_realistic_discussion_distractors(tmp_path):
    first, a = _financial_report("Aster", 2034, reverse=False)
    second, b = _financial_report("Birch", 2027, reverse=True)
    assert len(first) == len(second) == 120
    questions = [
        ("What was Aster's gross margin in FY2034?", {a["income"]}),
        ("What was Aster's working capital in FY2034?", {a["balance"]}),
        ("What was Aster's return on assets in FY2034?", {a["income"], a["balance"]}),
        ("What was Aster's cash conversion cycle in FY2034?", {a["income"], a["balance"]}),
        ("Determine Aster's operating margin for 2034.", {a["income"]}),
        ("How much was Aster's current ratio at the end of 2034?", {a["balance"]}),
        ("Which amounts determine Aster's return on equity for 2034?", {a["income"], a["balance"]}),
        ("Compute Aster's debt-to-equity ratio at year end 2034.", {a["balance"]}),
        ("How large was Birch's gross profit margin during 2027?", {b["income"]}),
        ("Calculate net working capital for Birch at the end of 2027.", {b["balance"]}),
        ("What inputs determine Birch's asset turnover for 2027?", {b["income"], b["balance"]}),
        ("What is the 2027 inventory turnover for Birch?", {b["income"], b["balance"]}),
        ("Calculate the acid-test quick ratio for Birch in 2027.", {b["balance"]}),
        ("How much free cash flow did Birch generate during 2027?", {b["cash"]}),
        ("Find Birch's net margin for the year 2027.", {b["income"]}),
        ("Estimate the cash operating cycle for Birch in 2027.", {b["income"], b["balance"]}),
    ]
    database = tmp_path / "metrics.db"
    (scope,) = asyncio.run(_load(database, (first + second,)))
    # _inspect reopens the database and checks every returned citation against its
    # persisted Source, document digest and exact physical page before returning.
    inspected = asyncio.run(_inspect(database, scope, [(question, 20) for question, _ in questions]))
    rows = []
    failures = []
    hits = total = 0
    for (question, expected), locations in zip(questions, inspected["results"], strict=True):
        actual = {(document_id, page_number) for document_id, _, page_number in locations}
        hits += len(actual & expected)
        total += len(expected)
        rows.append({"question": question, "hits": len(actual & expected), "evidence_pages": len(expected)})
        if not expected <= actual:
            failures.append({"question": question, "missing": sorted(expected - actual)})
    print(
        json.dumps({
            "dataset": "self_constructed_metric_operands_not_FinanceBench",
            "pages": 240,
            "questions": len(questions),
            "hits": hits,
            "evidence_pages": total,
            "queries": rows,
        })
    )
    assert not failures, failures
