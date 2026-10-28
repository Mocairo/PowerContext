"""Filesystem importers that turn project files into assets and chunks."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from labrelay.domain.models import Asset, Chunk
from labrelay.importers.chunking import split_lines, split_markdown
from labrelay.importers.pdf import extract_pdf_chunks, extract_pdf_page_texts
from labrelay.importers.security import redact_text, scan_text

# Mirrors ``file_import`` in configs/security.yaml; ``.pdf`` was added to that list
# because PDF page text is extracted and imported as evidence. The credential
# patterns in labrelay.importers.security are pinned against the same file, and
# ``tests/unit/test_importers.py::test_import_constants_match_security_yaml`` keeps
# both sides honest.
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".cache",
    "wandb",
    "checkpoints",
    "datasets",
}
TEXT_EXTENSIONS = {
    ".md",
    ".txt",
    ".rst",
    ".py",
    ".ipynb",
    ".yaml",
    ".yml",
    ".json",
    ".jsonl",
    ".toml",
    ".ini",
    ".cfg",
    ".csv",
    ".sh",
    ".sql",
    ".js",
    ".ts",
    ".pdf",
}
MAX_FILE_BYTES = 1_048_576
SKIP_BINARY_FILES = True
FOLLOW_SYMLINKS = False
BINARY_PROBE_BYTES = 8192
KIND_BY_NAME = {
    "train.py": "code",
    "evaluate.py": "code",
    "baseline.yaml": "config",
}
# Extension rules are decided before the keyword rules below, so a file only
# because it is called ``observers.pdf`` is still a paper, not an operations doc.
PAPER_SUFFIXES = {".pdf"}
CODE_SUFFIXES = {".py", ".ipynb", ".js", ".ts", ".sh", ".sql"}
CONFIG_SUFFIXES = {".yaml", ".yml", ".toml", ".ini", ".cfg"}
# Whole name tokens, not substrings: "observer" contains "server" but is not an
# operations document.
_NAME_TOKENS = re.compile(r"[^a-z0-9]+")
_OPERATIONS_WORDS = frozenset(
    {"operation", "operations", "server", "servers", "resource", "resources"}
)
_CONFIG_WORDS = frozenset({"config", "configs", "setting", "settings"})
_EXPERIMENT_WORDS = frozenset(
    {"metric", "metrics", "result", "results", "experiment", "experiments"}
)
# Checked in order, so the original keyword precedence is preserved.
KEYWORD_KINDS: tuple[tuple[frozenset[str], str], ...] = (
    (_OPERATIONS_WORDS, "operations"),
    (_CONFIG_WORDS, "config"),
    (_EXPERIMENT_WORDS, "experiment"),
)

logger = logging.getLogger("labrelay.importers.assets")


def classify_asset(path: Path) -> tuple[str, str]:
    """Return ``(title, kind)`` for an importable file."""
    lower = path.name.lower()
    if lower in KIND_BY_NAME:
        return path.name, KIND_BY_NAME[lower]
    suffix = path.suffix.lower()
    if suffix in PAPER_SUFFIXES:
        return path.name, "paper"
    if suffix in CODE_SUFFIXES:
        return path.name, "code"
    if suffix in CONFIG_SUFFIXES:
        return path.name, "config"
    tokens = frozenset(token for token in _NAME_TOKENS.split(lower.rsplit(".", 1)[0]) if token)
    for keywords, kind in KEYWORD_KINDS:
        if tokens & keywords:
            return path.name, kind
    return path.name, "note"


def _looks_binary(path: Path) -> bool:
    """Detect binary payloads by content, not by file extension."""
    try:
        with path.open("rb") as handle:
            sample = handle.read(BINARY_PROBE_BYTES)
    except OSError:
        return True
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError as exc:
        # A multi-byte character cut in half by the probe window is still text.
        return not (exc.reason == "unexpected end of data" and exc.start >= len(sample) - 4)
    return False


def skip_reason(path: Path, root: Path) -> str | None:
    """Return why ``path`` is not importable, or ``None`` when it is."""
    if path.is_dir():
        return "directory"
    try:
        relative_parts = path.relative_to(root).parts
    except ValueError:
        relative_parts = path.parts
    if any(part in EXCLUDED_DIRS for part in relative_parts):
        return "excluded_directory"
    if not FOLLOW_SYMLINKS and path.is_symlink():
        return "symlink"
    suffix = path.suffix.lower()
    if suffix not in TEXT_EXTENSIONS:
        return "unsupported_extension"
    try:
        size = path.stat().st_size
    except OSError as exc:
        return f"unreadable: {exc.strerror or exc.__class__.__name__}"
    if size > MAX_FILE_BYTES:
        return "too_large"
    # A PDF is a binary container by definition, so the content probe only applies
    # to the extensions that are supposed to hold plain text.
    if SKIP_BINARY_FILES and suffix != ".pdf" and _looks_binary(path):
        return "binary_content"
    return None


def scan_directory(root: Path) -> tuple[list[Path], list[tuple[Path, str]]]:
    """Split everything under ``root`` into ``(importable, skipped_with_reason)``.

    Directories themselves are not reported as skips; only real candidate files are,
    so ``skipped`` counts the handoff material the importer deliberately left out.
    """
    importable: list[Path] = []
    skipped: list[tuple[Path, str]] = []
    try:
        candidates = sorted(root.rglob("*"))
    except OSError as exc:
        logger.warning(
            "directory scan failed operation=scan_directory path=%s reason=%s", root, exc
        )
        return importable, skipped
    for path in candidates:
        reason = skip_reason(path, root)
        if reason is None:
            importable.append(path)
        elif reason != "directory":
            skipped.append((path, reason))
    return importable, skipped


def iter_importable_files(root: Path) -> Iterator[Path]:
    """Yield importable text files under ``root`` in a stable order."""
    yield from scan_directory(root)[0]


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


def asset_id_for(project_id: str, resolved_path: Path) -> str:
    """Stable asset identity: one project may never re-own another project's asset."""
    digest = hashlib.sha256(f"{project_id}\n{resolved_path}".encode()).hexdigest()
    return f"asset-{digest[:16]}"


def _credential_findings(path: Path) -> list[str]:
    """Scan the raw text of a file, including extracted PDF page text."""
    if path.suffix.lower() == ".pdf":
        try:
            pages = extract_pdf_page_texts(path)
        except ValueError:
            # An unreadable or text-less PDF is reported as failed while chunking.
            return []
        return scan_text("\n".join(content for _, content in pages))
    return scan_text(path.read_text(encoding="utf-8", errors="replace"))


def build_asset(project_id: str, path: Path) -> Asset:
    """Describe a file as an asset; detected secrets restrict it."""
    title, kind = classify_asset(path)
    resolved = path.resolve()
    findings = _credential_findings(path)
    status: Literal["active", "restricted", "failed"] = "restricted" if findings else "active"
    return Asset(
        id=asset_id_for(project_id, resolved),
        project_id=project_id,
        kind=kind,
        title=title,
        uri=str(resolved),
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
    ports from ``labrelay.domain.ports``. Asset identity is derived from the project
    plus the canonical resolved location, so the same folder can be handed to two
    projects without either re-owning the other's assets, and re-importing one
    project updates in place rather than duplicating.

    Only ``active`` evidence reaches the search engine: files whose content trips a
    credential pattern are persisted as ``restricted`` and never chunked or indexed,
    and a file that cannot be chunked is stored as ``status="failed"`` with the
    reason logged. Unreadable files and skipped files are counted, never fatal, so
    one locked file cannot abort an import that has already persisted assets.

    Returns ``assets`` (asset records persisted by this run, restricted included),
    ``chunks`` (chunks indexed), ``failed`` (files that could not be imported) and
    ``skipped`` (files left out by the ``configs/security.yaml`` discovery rules).
    """
    if not source_path.is_dir():
        raise ValueError(f"Import source {source_path} is not an existing directory")

    importable, skipped = scan_directory(source_path)
    for path, reason in skipped:
        logger.info(
            "import skipped operation=import_directory project_id=%s path=%s reason=%s",
            project_id,
            path,
            reason,
        )

    indexed_assets: list[Asset] = []
    indexed_chunks: list[Chunk] = []
    persisted = 0
    failed = 0

    for path in importable:
        try:
            asset = build_asset(project_id, path)
        except (OSError, ValueError) as exc:
            failed += 1
            logger.warning(
                "import failed operation=import_directory project_id=%s path=%s reason=%s",
                project_id,
                path,
                exc,
            )
            continue

        if asset.status == "restricted":
            await repository.save_asset(asset)
            persisted += 1
            logger.info(
                "asset restricted operation=import_directory project_id=%s asset_id=%s "
                "path=%s reason=credential_detected indexed=false",
                project_id,
                asset.id,
                path,
            )
            continue

        try:
            asset_chunks = chunk_asset(asset.id, path)
        except (OSError, ValueError) as exc:
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
        persisted += 1
        indexed_assets.append(asset)
        indexed_chunks.extend(asset_chunks)

    await search_engine.upsert_chunks(indexed_chunks, indexed_assets)
    return {
        "assets": persisted,
        "chunks": len(indexed_chunks),
        "failed": failed,
        "skipped": len(skipped),
    }
