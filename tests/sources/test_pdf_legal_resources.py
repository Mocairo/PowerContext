"""Legal null resources and ExtGState font selection preserve complete pages."""

from __future__ import annotations

import io

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NullObject,
    NumberObject,
    RectangleObject,
)

from powercontext.sources.pdf_pages import PDFDocumentInput, parse_pdf_document


def _font():
    return DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })


def _finish(writer, resources, content):
    page = writer.add_blank_page(width=300, height=300)
    page[NameObject("/Resources")] = resources
    stream = DecodedStreamObject()
    stream.set_data(content)
    page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    return PDFDocumentInput(document_id="legal-resources", content=buffer.getvalue())


def _state_resources(writer, font_setting=None):
    if font_setting is None:
        font_setting = ArrayObject([writer._add_object(_font()), FloatObject(10)])
    state = DictionaryObject({NameObject("/Type"): NameObject("/ExtGState"), NameObject("/Font"): font_setting})
    return DictionaryObject({
        NameObject("/ExtGState"): DictionaryObject({NameObject("/GS1"): writer._add_object(state)})
    })


@pytest.mark.parametrize("indirect", [False, True])
def test_null_resources_are_absent_on_a_vector_only_page(indirect):
    writer = PdfWriter()
    resources = writer._add_object(NullObject()) if indirect else NullObject()
    pages = parse_pdf_document(_finish(writer, resources, b"q 1 0 0 1 0 0 cm Q"))
    assert [(page.page_number, page.text) for page in pages] == [(1, "")]


def test_null_font_dictionary_is_absent_on_a_vector_only_page():
    writer = PdfWriter()
    resources = DictionaryObject({NameObject("/Font"): NullObject()})
    pages = parse_pdf_document(_finish(writer, resources, b"q 1 0 0 1 0 0 cm Q"))
    assert pages[0].text == ""


def test_extgstate_can_select_the_text_font():
    writer = PdfWriter()
    pages = parse_pdf_document(
        _finish(writer, _state_resources(writer), b"BT /GS1 gs 10 250 Td (Complete original text 123) Tj ET")
    )
    assert pages[0].text == "Complete original text 123"


def test_extgstate_font_selection_preserves_graphics_save_restore():
    writer = PdfWriter()
    resources = _state_resources(writer)
    resources[NameObject("/Font")] = DictionaryObject({NameObject("/F1"): writer._add_object(_font())})
    pages = parse_pdf_document(
        _finish(
            writer,
            resources,
            b"BT /F1 10 Tf 10 250 Td (Before 12) Tj ET q /GS1 gs BT 10 230 Td (Inside 34) Tj ET Q BT 10 210 Td (After 56) Tj ET",
        )
    )
    assert pages[0].text.splitlines() == ["Before 12", "Inside 34", "After 56"]


def test_restoring_state_without_a_font_does_not_retain_extgstate_font():
    writer = PdfWriter()
    with pytest.raises(ValueError, match="no usable font"):
        parse_pdf_document(_finish(writer, _state_resources(writer), b"q /GS1 gs Q BT (Missing font) Tj ET"))


def test_extgstate_font_works_in_a_form_xobject():
    writer = PdfWriter()
    form = DecodedStreamObject()
    form.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Form"),
        NameObject("/BBox"): RectangleObject([0, 0, 300, 300]),
        NameObject("/Resources"): _state_resources(writer),
    })
    form.set_data(b"BT /GS1 gs 10 250 Td (Form original text 789) Tj ET")
    resources = DictionaryObject({
        NameObject("/XObject"): DictionaryObject({NameObject("/Form1"): writer._add_object(form)})
    })
    pages = parse_pdf_document(_finish(writer, resources, b"/Form1 Do"))
    assert pages[0].text.strip() == "Form original text 789"


@pytest.mark.parametrize(
    "font_setting", [NumberObject(42), ArrayObject([]), ArrayObject([NumberObject(42), FloatObject(10)])]
)
def test_malformed_extgstate_font_fails_atomically(font_setting):
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    with pytest.raises(ValueError):
        parse_pdf_document(
            _finish(
                writer,
                _state_resources(writer, font_setting),
                b"BT /GS1 gs 10 250 Td (Must not return partial pages) Tj ET",
            )
        )


def _unicode_font(writer, character):
    font = _font()
    mapping = DecodedStreamObject()
    mapping.set_data(
        b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        b"/CMapName /UnicodeMap def /CMapType 2 def\n"
        b"1 begincodespacerange <00> <FF> endcodespacerange\n"
        + f"1 beginbfchar <41> <{ord(character):04X}> endbfchar\n".encode("ascii")
        + b"endcmap CMapName currentdict /CMap defineresource pop end end"
    )
    font[NameObject("/ToUnicode")] = writer._add_object(mapping)
    return writer._add_object(font)


def test_extgstate_font_applies_to_unicode_mapping_and_preserves_original_digest():
    import hashlib

    writer = PdfWriter()
    resources = _state_resources(writer, ArrayObject([_unicode_font(writer, "财"), FloatObject(10)]))
    value = _finish(writer, resources, b"BT /GS1 gs 10 250 Td (A 123) Tj ET")
    pages = parse_pdf_document(value)
    assert pages[0].text == "财 123"
    assert pages[0].document_sha256 == hashlib.sha256(value.content).hexdigest()
    assert parse_pdf_document(value) == pages


def test_unicode_extgstate_fonts_are_restored_by_graphics_state():
    writer = PdfWriter()
    resources = _state_resources(writer, ArrayObject([_unicode_font(writer, "财"), FloatObject(10)]))
    states = resources["/ExtGState"]
    assert isinstance(states, DictionaryObject)
    states[NameObject("/GS2")] = DictionaryObject({
        NameObject("/Font"): ArrayObject([_unicode_font(writer, "税"), FloatObject(10)])
    })
    pages = parse_pdf_document(
        _finish(
            writer,
            resources,
            b"/GS1 gs BT 10 250 Td (A 12) Tj ET q /GS2 gs BT 10 230 Td (A 34) Tj ET Q BT 10 210 Td (A 56) Tj ET",
        )
    )
    assert pages[0].text.splitlines() == ["财 12", "税 34", "财 56"]


@pytest.mark.parametrize("own_resources", [False, True])
def test_reused_form_inherits_each_callers_unicode_font_without_duplicate_text(own_resources):
    writer = PdfWriter()
    resources = _state_resources(writer, ArrayObject([_unicode_font(writer, "财"), FloatObject(10)]))
    states = resources["/ExtGState"]
    assert isinstance(states, DictionaryObject)
    states[NameObject("/GS2")] = DictionaryObject({
        NameObject("/Font"): ArrayObject([_unicode_font(writer, "税"), FloatObject(10)])
    })
    form = DecodedStreamObject()
    form.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Form"),
        NameObject("/BBox"): RectangleObject([0, 0, 300, 300]),
    })
    if own_resources:
        form[NameObject("/Resources")] = DictionaryObject()
    form.set_data(b"BT 10 250 Td (A 789) Tj ET")
    resources[NameObject("/XObject")] = DictionaryObject({
        NameObject("/Form1"): writer._add_object(form.flate_encode())
    })
    pages = parse_pdf_document(_finish(writer, resources, b"/GS1 gs /Form1 Do q /GS2 gs /Form1 Do Q /Form1 Do"))
    assert pages[0].text.splitlines() == ["财 789", "税 789", "财 789"]
