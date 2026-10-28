"""Page-level extraction for PDF evidence."""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from labrelay.domain.models import Chunk


def extract_pdf_chunks(path: Path, asset_id: str) -> list[Chunk]:
    """Return one chunk per PDF page that carries extractable text.

    Raises ``ValueError`` with an actionable reason when the file cannot be read
    or holds no text at all, so the caller can report a failed import instead of
    indexing an empty asset.
    """
    chunks: list[Chunk] = []
    try:
        reader = PdfReader(str(path))
        for page_number, page in enumerate(reader.pages, start=1):
            content = page.extract_text() or ""
            if content.strip():
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
    except PyPdfError as exc:
        raise ValueError(f"PDF {path.name} could not be read: {exc}") from exc

    if not chunks:
        raise ValueError(
            f"PDF {path.name} contains no extractable text; OCR is required before importing it"
        )
    return chunks
