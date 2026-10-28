"""Filesystem importers that turn project files into assets and chunks."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from labrelay.domain.models import Asset, Chunk
from labrelay.importers.chunking import split_lines, split_markdown
from labrelay.importers.pdf import extract_pdf_chunks
from labrelay.importers.security import redact_text, scan_text

# Mirrors ``file_import`` in configs/security.yaml; the credential patterns in
# labrelay.importers.security are pinned against that file by a unit test.
EXCLUDED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".cache"}
TEXT_EXTENSIONS = {
    ".md",
    ".txt",
    ".py",
    ".yaml",
    ".yml",
    ".json",
    ".jsonl",
    ".csv",
    ".ipynb",
    ".pdf",
}
MAX_FILE_BYTES = 1_048_576
KIND_BY_NAME = {
    "train.py": "code",
    "evaluate.py": "code",
    "baseline.yaml": "config",
}

logger = logging.getLogger("labrelay.importers.assets")


def classify_asset(path: Path) -> tuple[str, str]:
    """Return ``(title, kind)`` for an importable file."""
    lower = path.name.lower()
    if lower in KIND_BY_NAME:
        return path.name, KIND_BY_NAME[lower]
    if "operation" in lower or "server" in lower or "resource" in lower:
        return path.name, "operations"
    if lower.endswith(".pdf"):
        return path.name, "paper"
    if "config" in lower or lower.endswith((".yaml", ".yml")):
        return path.name, "config"
    if "metric" in lower or "result" in lower or "experiment" in lower:
        return path.name, "experiment"
    if lower.endswith(".py") or lower.endswith(".ipynb"):
        return path.name, "code"
    return path.name, "note"


def iter_importable_files(root: Path) -> Iterator[Path]:
    """Yield importable text files under ``root`` in a stable order."""
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            continue
        yield path


def chunk_asset(asset_id: str, path: Path) -> list[Chunk]:
    """Split one file into citation-bearing chunks, redacting credentials."""
    if path.suffix.lower() == ".pdf":
        return extract_pdf_chunks(path, asset_id)
    text = redact_text(path.read_text(encoding="utf-8", errors="replace"))
    if path.suffix.lower() == ".md":
        return split_markdown(text, asset_id=asset_id)
    return split_lines(text, asset_id=asset_id)


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_asset(project_id: str, path: Path) -> Asset:
    """Describe a file as an asset; detected secrets restrict it."""
    title, kind = classify_asset(path)
    status: Literal["active", "restricted", "failed"] = "active"
    if path.suffix.lower() != ".pdf":
        findings = scan_text(path.read_text(encoding="utf-8", errors="replace"))
        status = "restricted" if findings else "active"
    return Asset(
        id=f"asset-{hashlib.sha256(str(path).encode()).hexdigest()[:16]}",
        project_id=project_id,
        kind=kind,
        title=title,
        uri=str(path),
        checksum=checksum(path),
        status=status,
    )


async def import_directory(
    repository,
    search_engine,
    project_id: str,
    source_path: Path,
) -> dict[str, int]:
    """Import ``source_path`` into ``project_id`` and index the resulting chunks.

    ``repository`` and ``search_engine`` are the ``Repository`` and ``SearchEngine``
    ports from ``labrelay.domain.ports``. Asset and chunk identifiers are derived from
    the file path, so importing the same directory twice updates in place rather than
    duplicating. A file whose content cannot be chunked is stored as ``status="failed"``
    with the reason logged, and never indexed.
    """
    assets: list[Asset] = []
    chunks: list[Chunk] = []
    failed = 0

    for path in iter_importable_files(source_path):
        asset = build_asset(project_id, path)
        try:
            asset_chunks = chunk_asset(asset.id, path)
        except ValueError as exc:
            failed += 1
            logger.warning(
                "import failed operation=import_directory project_id=%s asset_id=%s "
                "path=%s reason=%s",
                project_id,
                asset.id,
                path,
                exc,
            )
            await repository.save_asset(asset.model_copy(update={"status": "failed"}))
            continue
        await repository.save_asset(asset)
        assets.append(asset)
        chunks.extend(asset_chunks)

    await search_engine.upsert_chunks(chunks, assets)
    return {"assets": len(assets), "chunks": len(chunks), "failed": failed}
