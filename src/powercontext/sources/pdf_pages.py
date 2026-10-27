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

"""Complete offline PDF extraction and durable, revision-specific page Sources."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import threading

from pydantic import BaseModel, Field, field_validator
from pypdf import PdfReader
from pypdf.generic import ArrayObject, ContentStream, DictionaryObject, PdfObject, StreamObject
from typing_extensions import override

from powercontext.sources._pdf_search import search_pdf_pages
from powercontext.sources.definitions import SourceDefinition, SourceProjection
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


def _document_id(value: str) -> str:
    if not value.strip():
        raise ValueError("document_id must not be blank")  # noqa: TRY003
    return value


class PDFDocumentInput(BaseModel):
    document_id: str
    content: bytes

    _validate_document_id = field_validator("document_id")(_document_id)


class PDFPageInput(BaseModel):
    document_id: str
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_number: int = Field(gt=0, strict=True)
    text: str

    _validate_document_id = field_validator("document_id")(_document_id)


class PDFPageSource(Source):
    document_id: str
    document_sha256: str
    page_number: int
    text: str


class _ExtractionWarnings(logging.Handler):
    """Detect pypdf's logged recovery paths without changing logger levels."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.thread_id = threading.get_ident()
        self.messages: list[str] = []

    @override
    def emit(self, record: logging.LogRecord) -> None:
        if record.thread == self.thread_id:
            self.messages.append(record.getMessage())


def _page_tree_count(node: PdfObject, ancestors: frozenset[int] = frozenset()) -> int:
    resolved = node.get_object()
    if not isinstance(resolved, DictionaryObject) or id(resolved) in ancestors:
        raise ValueError("Invalid or cyclic PDF page tree")  # noqa: TRY003
    if resolved["/Type"] == "/Page":
        return 1
    if resolved["/Type"] != "/Pages" or not isinstance(resolved["/Kids"], ArrayObject):
        raise ValueError("Invalid PDF page tree node")  # noqa: TRY003
    count = sum(_page_tree_count(child, ancestors | {id(resolved)}) for child in resolved["/Kids"])
    if resolved["/Count"] != count:
        raise ValueError("PDF page tree count does not match its physical pages")  # noqa: TRY003
    return count


def _validate_text_resources(  # noqa: C901 - PDF graphics state and resource operators are checked together.
    contents: ContentStream,
    resources: PdfObject,
    reader: PdfReader,
    ancestors: frozenset[int] = frozenset(),
    font_selected: bool = False,
) -> None:
    """Check resource failures that pypdf otherwise silently turns into missing text."""
    if not isinstance(resources, DictionaryObject):
        raise TypeError("Invalid PDF page resources")  # noqa: TRY003
    fonts = resources.get("/Font", DictionaryObject()).get_object()
    if not isinstance(fonts, DictionaryObject):
        raise TypeError("Invalid PDF font resources")  # noqa: TRY003
    stack = []
    for operands, operator in contents.operations:
        if operator == b"q":
            stack.append(font_selected)
        elif operator == b"Q" and stack:
            font_selected = stack.pop()
        elif operator == b"Tf":
            font = fonts[operands[0]].get_object()
            if not isinstance(font, DictionaryObject) or not font.get("/Subtype"):
                raise ValueError("Invalid PDF text font")  # noqa: TRY003
            font_selected = True
        elif operator in (b"Tj", b"TJ", b"'", b'"') and not font_selected:
            raise ValueError("PDF text has no usable font")  # noqa: TRY003
        elif operator == b"Do":
            objects = resources["/XObject"]
            if not isinstance(objects, DictionaryObject):
                raise TypeError("Invalid PDF XObject resources")  # noqa: TRY003
            form = objects[operands[0]].get_object()
            if not isinstance(form, StreamObject):
                raise ValueError("Invalid PDF XObject stream")  # noqa: TRY003
            if form.get("/Subtype") == "/Form":
                if id(form) in ancestors:
                    raise ValueError("Cyclic PDF Form XObject")  # noqa: TRY003
                _validate_text_resources(
                    ContentStream(form, reader),
                    form.get("/Resources", resources).get_object(),
                    reader,
                    ancestors | {id(form)},
                    font_selected,
                )


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Extract every physical page before returning; never return partial text.

    Strict parsing rejects damaged cross references. Explicit content parsing and
    warning checks also catch errors that pypdf otherwise converts to empty text
    or a partially decoded Form XObject.
    """
    _document_id(value.document_id)
    if not value.content.startswith(b"%PDF-") or not value.content.rstrip().endswith(b"%%EOF"):
        raise ValueError("content must be a complete PDF file")  # noqa: TRY003
    digest = hashlib.sha256(value.content).hexdigest()
    warnings = _ExtractionWarnings()
    logger = logging.getLogger("pypdf")
    logger.addHandler(warnings)
    try:
        with io.BytesIO(value.content) as stream:
            reader = PdfReader(stream, strict=True)
            if reader.is_encrypted and not reader.decrypt(""):
                raise ValueError("PDF requires a password")  # noqa: TRY003, TRY301
            expected_count = _page_tree_count(reader.root_object["/Pages"])
            pages = []
            for number, page in enumerate(reader.pages, 1):
                # extract_text can silently treat malformed content as blank.
                contents = page.get_contents()
                if contents is not None:
                    _validate_text_resources(contents, page.get("/Resources", DictionaryObject()).get_object(), reader)
                text = page.extract_text()
                pages.append(
                    PDFPageInput(
                        document_id=value.document_id,
                        document_sha256=digest,
                        page_number=number,
                        text=text,
                    )
                )
            if not pages or len(pages) != expected_count:
                raise ValueError("PDF must contain all its declared physical pages")  # noqa: TRY003, TRY301
            if warnings.messages:
                raise ValueError(f"PDF could not be completely decoded: {warnings.messages[0]}")  # noqa: TRY003, TRY301
            return tuple(pages)
    except Exception as error:
        raise ValueError(f"Unable to parse complete PDF: {error}") from error  # noqa: TRY003
    finally:
        logger.removeHandler(warnings)
        warnings.close()


class _PDFPageTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type="pdf-page",
            source_id=source.name,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


class _PDFPageSourceDefinition:
    name = "pdf-page"
    version = "1"
    input_class = PDFPageInput
    source_class = PDFPageSource
    projections: tuple[SourceProjection[PDFPageSource], ...] = (_PDFPageTextEvidenceProjection(),)

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        # JSON frames all three components unambiguously, including arbitrary
        # Unicode document IDs. A digest bounds identity length independently of ID.
        page = PDFPageInput.model_validate(value.model_dump())
        identity = json.dumps(
            [page.document_id, page.document_sha256, page.page_number], ensure_ascii=True, separators=(",", ":")
        ).encode("ascii")
        return PDFPageSource(
            name=f"pdf:{hashlib.sha256(identity).hexdigest()}",
            materialization=SourceMaterialization.CAPTURED,
            **page.model_dump(),
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


PDF_PAGE_SOURCE_DEFINITION: SourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput] = _PDFPageSourceDefinition()

__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "parse_pdf_document",
    "search_pdf_pages",
]
