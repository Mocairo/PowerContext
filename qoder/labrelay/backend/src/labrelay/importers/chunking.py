"""Text splitting that keeps a citation locator on every chunk."""

from __future__ import annotations

from labrelay.domain.models import Chunk

MAX_LINES_PER_CHUNK = 80


def split_lines(text: str, *, asset_id: str, max_lines: int = MAX_LINES_PER_CHUNK) -> list[Chunk]:
    """Chunk code/config/CSV style files by line range."""
    lines = text.splitlines()
    chunks: list[Chunk] = []
    for start in range(0, len(lines), max_lines):
        block = lines[start : start + max_lines]
        end = start + len(block)
        content = "\n".join(block)
        if content.strip():
            chunks.append(
                Chunk(
                    id=f"{asset_id}:lines:{start + 1}-{end}",
                    asset_id=asset_id,
                    ordinal=len(chunks),
                    content=content,
                    locator={"line_start": start + 1, "line_end": end},
                    token_count=max(1, len(content) // 4),
                )
            )
    return chunks


def split_markdown(text: str, *, asset_id: str) -> list[Chunk]:
    """Chunk notes by Markdown heading so a citation can name its section.

    The heading line stays inside the chunk content (a quoted excerpt therefore
    carries its own section title), while the locator keeps the bare heading text.
    """
    chunks: list[Chunk] = []
    heading = "document"
    heading_line: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal heading_line
        body = "\n".join(buffer).strip()
        buffer.clear()
        if not body:
            return
        content = f"{heading_line}\n{body}" if heading_line else body
        chunks.append(
            Chunk(
                id=f"{asset_id}:heading:{len(chunks)}",
                asset_id=asset_id,
                ordinal=len(chunks),
                content=content,
                locator={"section": heading},
                token_count=max(1, len(content) // 4),
            )
        )
        heading_line = None

    for line in text.splitlines():
        if line.startswith("#"):
            flush()
            heading = line.lstrip("#").strip() or "document"
            heading_line = line
        else:
            buffer.append(line)
    flush()
    return chunks
