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
    """Chunk notes by Markdown heading so a citation can name its section."""
    chunks: list[Chunk] = []
    heading = "document"
    buffer: list[str] = []

    def flush() -> None:
        content = "\n".join(buffer).strip()
        if content:
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
        buffer.clear()

    for line in text.splitlines():
        if line.startswith("#"):
            flush()
            heading = line.lstrip("#").strip()
        else:
            buffer.append(line)
    flush()
    return chunks
