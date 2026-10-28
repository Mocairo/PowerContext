"""Page-level extraction for PDF evidence."""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from labrelay.domain.models import Chunk
from labrelay.importers.security import redact_text


def extract_pdf_page_texts(path: Path) -> list[tuple[int, str]]:
    """Return ``(page_number, raw text)`` for every page carrying extractable text.

    The text is un-redacted and is only meant for credential scanning: anything
    that reaches an index or a citation must go through :func:`redact_text` first.

    Raises ``ValueError`` with an actionable reason when the file cannot be read
    or holds no text at all, so the caller can report a failed import instead of
    indexing an empty asset.
    """
    pages: list[tuple[int, str]] = []
    try:
        reader = PdfReader(str(path))
        for page_number, page in enumerate(reader.pages, start=1):
            content = page.extract_text() or ""
            if content.strip():
                pages.append((page_number, content))
    except PyPdfError as exc:
        raise ValueError(f"PDF {path.name} could not be read: {exc}") from exc

    if not pages:
        raise ValueError(
            f"PDF {path.name} contains no extractable text; OCR is required before importing it"
        )
    return pages


def extract_pdf_chunks(path: Path, asset_id: str) -> list[Chunk]:
    """Return one redacted chunk per PDF page that carries extractable text."""
    chunks: list[Chunk] = []
    for page_number, raw_content in extract_pdf_page_texts(path):
        content = redact_text(raw_content)
        chunks.append(
            Chunk(
                id=f"{asset_id}:page:{page_number}",
                asset_id=asset_id,
                ordinal=len(chunks),
                content=content,
                locator={"page": page_number},
                token_count=max(1, len(content) // 4),
            )
        )
    return chunks
