"""Filesystem importers that turn project files into assets and chunks."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterator
from pathlib import Path
from stat import S_ISDIR
from typing import Literal, TypedDict

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


def _oserror_detail(exc: OSError) -> str:
    """A short, credential-free description of an OS failure.

    Only ``strerror`` (the OS message, e.g. "permission denied") is used; the file
    path already travels in its own ``path=`` field, so ``detail=`` never repeats it
    and never carries file content.
    """
    return exc.strerror or exc.__class__.__name__


def skip_reason(path: Path, root: Path) -> str | None:
    """Return why ``path`` is not importable, or ``None`` when it is."""
    return _skip_info(path, root)[0]


def _skip_info(path: Path, root: Path) -> tuple[str | None, str | None]:
    """Return ``(reason, detail)`` for a candidate file.

    ``reason`` is always one stable single token (``unreadable``, never
    ``unreadable: <os message>``) so it stays machine-comparable; the OS message goes
    to the separate ``detail`` field of the log line and nowhere else.
    """
    try:
        info = path.stat()
    except OSError as exc:
        return "unreadable", _oserror_detail(exc)
    if S_ISDIR(info.st_mode):
        return "directory", None
    try:
        relative_parts = path.relative_to(root).parts
    except ValueError:
        relative_parts = path.parts
    if any(part in EXCLUDED_DIRS for part in relative_parts):
        return "excluded_directory", None
    if not FOLLOW_SYMLINKS and path.is_symlink():
        return "symlink", None
    suffix = path.suffix.lower()
    if suffix not in TEXT_EXTENSIONS:
        return "unsupported_extension", None
    if info.st_size > MAX_FILE_BYTES:
        return "too_large", None
    # A PDF is a binary container by definition, so the content probe only applies
    # to the extensions that are supposed to hold plain text.
    if SKIP_BINARY_FILES and suffix != ".pdf" and _looks_binary(path):
        return "binary_content", None
    return None, None


def scan_directory(root: Path) -> tuple[list[Path], list[tuple[Path, str, str | None]]]:
    """Split everything under ``root`` into ``(importable, skipped)``.

    Directories themselves are not reported as skips; only real candidate files are,
    so ``skipped`` counts the handoff material the importer deliberately left out.
    Each skip is ``(path, reason, detail)``, where ``reason`` is a stable token and
    ``detail`` is filled only for the ``unreadable`` reason.
    """
    importable: list[Path] = []
    skipped: list[tuple[Path, str, str | None]] = []
    try:
        candidates = sorted(root.rglob("*"))
    except OSError as exc:
        logger.warning(
            "directory scan failed operation=scan_directory path=%s reason=unreadable detail=%s",
            root,
            _oserror_detail(exc),
        )
        return importable, skipped
    for path in candidates:
        reason, detail = _skip_info(path, root)
        if reason is None:
            importable.append(path)
        elif reason != "directory":
            skipped.append((path, reason, detail))
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


def _asset_and_findings(project_id: str, path: Path) -> tuple[Asset, list[str]]:
    """Describe a file as an asset and return the credential patterns it matched.

    The finding names are the operator-facing reason a file was blocked, so they are
    returned here rather than re-derived by the caller: they come from the same scan
    that sets the status, and one file read buys both. They are pattern names
    (``api_secret``), never file content.
    """
    title, kind = classify_asset(path)
    resolved = path.resolve()
    findings = _credential_findings(path)
    status: Literal["active", "restricted", "failed"] = "restricted" if findings else "active"
    return (
        Asset(
            id=asset_id_for(project_id, resolved),
            project_id=project_id,
            kind=kind,
            title=title,
            uri=str(resolved),
            checksum=checksum(path),
            status=status,
        ),
        findings,
    )


def build_asset(project_id: str, path: Path) -> Asset:
    """Describe a file as an asset; detected secrets restrict it."""
    asset, _ = _asset_and_findings(project_id, path)
    return asset


class ImportResult(TypedDict):
    """Outcome of one import run, as returned by ``import_directory``.

    ``restricted`` counts the credential-bearing assets that were persisted but never
    indexed, and ``restricted_reasons`` maps each of those asset ids to the sorted
    finding names that blocked it, so ``len(restricted_reasons) == restricted``.
    """

    assets: int
    chunks: int
    failed: int
    skipped: int
    restricted: int
    restricted_reasons: dict[str, list[str]]


async def import_directory(
    repository,
    search_engine,
    project_id: str,
    source_path: Path,
) -> ImportResult:
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
    ``chunks`` (chunks indexed), ``failed`` (files that could not be imported),
    ``skipped`` (files left out by the ``configs/security.yaml`` discovery rules),
    ``restricted`` (assets stored but withheld from the index) and
    ``restricted_reasons`` (each restricted asset id mapped to its sorted credential
    finding names), so an operator can tell exactly how much evidence was blocked and
    why without any file content leaving the process.
    """
    if not source_path.is_dir():
        raise ValueError(f"Import source {source_path} is not an existing directory")

    importable, skipped = scan_directory(source_path)
    for path, reason, detail in skipped:
        logger.info(
            "import skipped operation=import_directory project_id=%s path=%s reason=%s detail=%s",
            project_id,
            path,
            reason,
            detail or "-",
        )

    indexed_assets: list[Asset] = []
    indexed_chunks: list[Chunk] = []
    restricted_reasons: dict[str, list[str]] = {}
    persisted = 0
    failed = 0

    for path in importable:
        try:
            asset, findings = _asset_and_findings(project_id, path)
        except (OSError, ValueError) as exc:
            failed += 1
            logger.warning(
                "import failed operation=import_directory project_id=%s path=%s "
                "reason=build_failed detail=%s",
                project_id,
                path,
                exc,
            )
            continue

        if asset.status == "restricted":
            await repository.save_asset(asset)
            persisted += 1
            # The blocked-file list is the point of this line: which asset, why, and
            # that it never reached the index. Findings are pattern names only.
            names = sorted(findings)
            restricted_reasons[asset.id] = names
            logger.info(
                "asset restricted operation=import_restricted_asset project_id=%s "
                "asset_id=%s path=%s findings=%s indexed=false",
                project_id,
                asset.id,
                path,
                ",".join(names),
            )
            continue

        try:
            asset_chunks = chunk_asset(asset.id, path)
        except (OSError, ValueError) as exc:
            failed += 1
            logger.warning(
                "import failed operation=import_directory project_id=%s asset_id=%s "
                "path=%s reason=chunk_failed detail=%s",
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
        "restricted": len(restricted_reasons),
        "restricted_reasons": restricted_reasons,
    }
