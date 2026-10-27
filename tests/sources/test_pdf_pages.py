"""Full PDF page fidelity, atomic failure and durable Source contracts."""

from __future__ import annotations

import asyncio
import hashlib
import io

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from powercontext.sources import SourceDefinitionRegistry, SourceMaterialization, project_source_for_transport
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    PDFPageInput,
    parse_pdf_document,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY
from tests.pdf_test_support import pdf_bytes, terms, text_metrics


def test_every_physical_page_keeps_complete_text_table_numbers_and_blank_pages():
    expected = (
        "Aster Industries Annual Report 2031\nPrinted page iv\nRevenue revenue revenue 1,234.56 (78) 0.03%",
        "",
        "Consolidated statements of cash flows\n2031 2030\nOperating cash 987 654\nCapital expenditures (321) (210)",
        "Printed page 1\nOnly physical page four contains the terminalword evidence.",
        "",
    )
    raw = pdf_bytes(expected)
    pages = parse_pdf_document(PDFDocumentInput(document_id="aster/2031", content=raw))
    assert isinstance(pages, tuple)
    assert len(pages) == len(expected)
    for index, (page, text) in enumerate(zip(pages, expected, strict=True), 1):
        assert isinstance(page, PDFPageInput)
        assert page.page_number == index
        assert page.document_id == "aster/2031"
        assert page.document_sha256 == hashlib.sha256(raw).hexdigest()
        assert terms(page.text) == terms(text)
        assert text_metrics(page.text, text) == (1.0, 1.0)
        if not text:
            assert page.text == ""
    assert text_metrics(pages[0].text, "Revenue revenue revenue 1,234.56")[0] == 1.0
    assert "terminalword" not in pages[2].text
    assert "terminalword" in pages[3].text


@pytest.mark.parametrize("document_id", ["", " ", "\t\r\n"])
def test_blank_document_ids_fail(document_id):
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id=document_id, content=pdf_bytes(("complete",))))


def test_nonblank_document_id_is_preserved_without_trimming():
    raw = pdf_bytes(("Original body",))
    page = parse_pdf_document(PDFDocumentInput(document_id=" report ", content=raw))[0]
    assert page.document_id == " report "


@pytest.mark.parametrize("raw", [b"", b"plain text", b"%PDF-1.7\ntruncated", b"<html>report</html>"])
def test_invalid_documents_fail_with_value_error(raw):
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="bad", content=raw))


def test_truncated_original_document_is_not_accepted_as_complete():
    raw = pdf_bytes(("First page", "Last page"))
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="truncated", content=raw[: raw.index(b"xref")]))


def test_invalid_later_page_contents_rejects_entire_document():
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_bytes(("Valid first page", "Second page")))))
    writer.pages[1][NameObject("/Contents")] = NumberObject(42)
    stream = io.BytesIO()
    writer.write(stream)
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="later-corruption", content=stream.getvalue()))


def test_corrupt_form_object_does_not_silently_drop_final_page_evidence():
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_bytes(("Valid first page", "Visible second page")))))
    page = writer.pages[1]
    resources = page["/Resources"]
    assert isinstance(resources, DictionaryObject)
    resources[NameObject("/XObject")] = DictionaryObject()
    original_contents = page.get_contents()
    assert original_contents is not None
    contents = DecodedStreamObject()
    contents.set_data(original_contents.get_data() + b"\n/MissingEvidence Do\n")
    page[NameObject("/Contents")] = writer._add_object(contents)
    stream = io.BytesIO()
    writer.write(stream)
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="corrupt-form", content=stream.getvalue()))


@pytest.mark.parametrize("password", ["", "secret"])
def test_permission_marker_is_accepted_only_when_no_password_is_needed(password):
    raw = pdf_bytes(("Restricted copying but readable text 42", ""), password=password)
    if password:
        with pytest.raises(ValueError):
            parse_pdf_document(PDFDocumentInput(document_id="permissions", content=raw))
    else:
        pages = parse_pdf_document(PDFDocumentInput(document_id="permissions", content=raw))
        assert len(pages) == 2
        assert text_metrics(pages[0].text, "Restricted copying but readable text 42") == (1.0, 1.0)
        assert pages[1].text == ""


def test_definition_projection_roundtrip_and_revision_identity():
    async def scenario():
        definition = PDF_PAGE_SOURCE_DEFINITION
        assert definition.name == "pdf-page" and definition.version == "1"
        assert definition.input_class is PDFPageInput
        registry = SourceDefinitionRegistry((definition,))
        original = pdf_bytes(("Revenue 100", "Revenue 200"), title="Original")
        revision = pdf_bytes(("Revenue 100", "Revenue 200"), title="Revised metadata only")
        identifiers = ["a:b", "a/b", "a%3Ab", "a", " a", "a ", "报告" * 1000, "é", "e\u0301"]
        names = set()
        for document_id in identifiers:
            for raw in (original, revision):
                for page in parse_pdf_document(PDFDocumentInput(document_id=document_id, content=raw)):
                    source = await registry.resolve(page)
                    assert source.materialization is SourceMaterialization.CAPTURED
                    assert source.definition_version == "1"
                    assert 0 < len(source.name) <= 256
                    assert source.name not in names
                    names.add(source.name)
                    assert (await registry.resolve(page.model_copy(deep=True))).name == source.name
                    assert await registry.read(source) == page
                    observation = project_source_for_transport(registry, source)
                    evidence = observation.projection(TEXT_EVIDENCE_PROJECTION_KEY)
                    assert evidence == {
                        "source_type": "pdf-page",
                        "source_id": source.name,
                        "content": f"{document_id}\n{page.text}",
                        "metadata": {
                            "document_id": document_id,
                            "document_sha256": hashlib.sha256(raw).hexdigest(),
                            "page_number": page.page_number,
                        },
                    }
                    restored = definition.source_class.model_validate_json(source.model_dump_json())
                    assert await registry.read(restored) == page
        assert len(names) == len(identifiers) * 4

    asyncio.run(scenario())


@pytest.mark.parametrize("claimed_count", [0, 1, 3])
def test_inconsistent_page_tree_count_is_rejected(claimed_count):
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_bytes(("First", "Second")))))
    tree = writer.root_object["/Pages"]
    assert isinstance(tree, DictionaryObject)
    tree[NameObject("/Count")] = NumberObject(claimed_count)
    stream = io.BytesIO()
    writer.write(stream)
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="broken-tree", content=stream.getvalue()))


@pytest.mark.parametrize("damage", ["missing-resources", "invalid-font"])
def test_text_with_broken_resources_is_not_returned_as_blank_or_replacement_characters(damage):
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_bytes(("Valid first page", "Financial evidence 456")))))
    if damage == "missing-resources":
        del writer.pages[1][NameObject("/Resources")]
    else:
        resources = writer.pages[1]["/Resources"]
        assert isinstance(resources, DictionaryObject)
        fonts = resources["/Font"]
        assert isinstance(fonts, DictionaryObject)
        fonts[NameObject("/F1")] = NumberObject(42)
    stream = io.BytesIO()
    writer.write(stream)
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="broken-font", content=stream.getvalue()))


def test_rotated_page_and_form_xobject_text_stay_on_their_physical_page():
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_bytes(("First page 100", "Second page 200")))))
    page = writer.pages[1]
    form = DecodedStreamObject()
    form.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Form"),
        NameObject("/BBox"): ArrayObject([NumberObject(0), NumberObject(0), NumberObject(612), NumberObject(792)]),
        NameObject("/Resources"): page["/Resources"].clone(writer),
    })
    form.set_data(b"BT /F1 10 Tf 40 700 Td (Form evidence 345.67) Tj ET")
    resources = page["/Resources"]
    assert isinstance(resources, DictionaryObject)
    resources[NameObject("/XObject")] = DictionaryObject({NameObject("/ReportForm"): writer._add_object(form)})
    original_contents = page.get_contents()
    assert original_contents is not None
    stream = DecodedStreamObject()
    stream.set_data(original_contents.get_data() + b"\n/ReportForm Do\n")
    page[NameObject("/Contents")] = writer._add_object(stream)
    page.rotate(90)
    output = io.BytesIO()
    writer.write(output)
    pages = parse_pdf_document(PDFDocumentInput(document_id="rotated-form", content=output.getvalue()))
    assert terms(pages[0].text) == terms("First page 100")
    assert terms(pages[1].text) == terms("Second page 200 Form evidence 345.67")


def test_unicode_font_mapping_is_preserved():
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_bytes(("AB 123",)))))
    mapping = DecodedStreamObject()
    mapping.set_data(b"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /ReportUnicode def
/CMapType 2 def
1 begincodespacerange
<00> <FF>
endcodespacerange
2 beginbfchar
<41> <8D22>
<42> <62A5>
endbfchar
endcmap
CMapName currentdict /CMap defineresource pop
end
end
""")
    resources = writer.pages[0]["/Resources"]
    assert isinstance(resources, DictionaryObject)
    fonts = resources["/Font"]
    assert isinstance(fonts, DictionaryObject)
    font = fonts["/F1"]
    assert isinstance(font, DictionaryObject)
    font[NameObject("/ToUnicode")] = writer._add_object(mapping)
    output = io.BytesIO()
    writer.write(output)
    pages = parse_pdf_document(PDFDocumentInput(document_id="unicode", content=output.getvalue()))
    assert pages[0].text == "财报 123"


def test_inherited_font_resources_remain_valid():
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_bytes(("First inherited 123", "Second inherited 456")))))
    tree = writer.root_object["/Pages"]
    assert isinstance(tree, DictionaryObject)
    tree[NameObject("/Resources")] = writer.pages[0]["/Resources"]
    for page in writer.pages:
        del page[NameObject("/Resources")]
    output = io.BytesIO()
    writer.write(output)
    pages = parse_pdf_document(PDFDocumentInput(document_id="inherited", content=output.getvalue()))
    assert [terms(page.text) for page in pages] == [terms("First inherited 123"), terms("Second inherited 456")]


def test_vector_only_and_image_only_pages_are_valid_without_ocr():
    writer = PdfWriter()
    vector = writer.add_blank_page(width=200, height=200)
    vector_stream = DecodedStreamObject()
    vector_stream.set_data(b"0 0 m 50 50 l S")
    vector[NameObject("/Contents")] = writer._add_object(vector_stream)
    image_page = writer.add_blank_page(width=200, height=200)
    raster = DecodedStreamObject()
    raster.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(1),
        NameObject("/Height"): NumberObject(1),
        NameObject("/BitsPerComponent"): NumberObject(8),
        NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
    })
    raster.set_data(b"\xff\x00\x00")
    image_page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/XObject"): DictionaryObject({NameObject("/Image"): writer._add_object(raster)})
    })
    image_stream = DecodedStreamObject()
    image_stream.set_data(b"q 100 0 0 100 0 0 cm /Image Do Q")
    image_page[NameObject("/Contents")] = writer._add_object(image_stream)
    output = io.BytesIO()
    writer.write(output)
    pages = parse_pdf_document(PDFDocumentInput(document_id="graphics", content=output.getvalue()))
    assert [(page.page_number, page.text) for page in pages] == [(1, ""), (2, "")]
