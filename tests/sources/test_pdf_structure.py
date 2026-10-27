"""Valid indirect page-tree objects must not be mistaken for damaged PDFs."""

from __future__ import annotations

import io

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject

from powercontext.sources.pdf_pages import PDFDocumentInput, parse_pdf_document
from tests.pdf_test_support import pdf_bytes


@pytest.mark.parametrize("indirect_key", ["/Kids", "/Count"])
def test_valid_indirect_page_tree_objects(indirect_key):
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(pdf_bytes(("Complete source text 123", "")))))
    tree = writer.root_object["/Pages"]
    tree[NameObject(indirect_key)] = writer._add_object(tree[indirect_key])
    stream = io.BytesIO()
    writer.write(stream)
    parsed = parse_pdf_document(PDFDocumentInput(document_id="indirect", content=stream.getvalue()))
    assert len(parsed) == 2
    assert [(page.page_number, page.text) for page in parsed] == [(1, "Complete source text 123"), (2, "")]
