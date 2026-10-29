"""Stage browser uploads into one directory the importer can read.

The importer reads from the filesystem, so an upload is two steps: write each accepted file to
``data/uploads/<project_id>/`` and then import that directory. Staging keeps one directory per
project, so re-uploading a file with the same name replaces it and the asset id stays stable —
the importer updates the asset in place instead of creating a duplicate.

Nothing here decides what an asset means: extension, size, binary and credential rules stay in
:mod:`labrelay.importers.assets`, and the security scan runs on the staged file exactly as it
runs on a hand-picked directory.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile

from labrelay.importers.assets import BINARY_PROBE_BYTES, MAX_FILE_BYTES, TEXT_EXTENSIONS
from labrelay.paths import data_root

#: One request carries at most this many files; the UI selects a folder, not a dataset.
MAX_UPLOAD_FILES = 20

#: Same ceiling as the importer, so a file accepted here is not silently skipped later.
MAX_UPLOAD_BYTES = MAX_FILE_BYTES

#: Long enough for a paper title, short enough for every filesystem in play.
MAX_NAME_CHARS = 120

_READ_CHUNK_BYTES = 64 * 1024
_UNSAFE_NAME = re.compile(r"[\x00-\x1f\x7f/\\]")
_SAFE_PROJECT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(slots=True)
class StagedFile:
    name: str
    path: Path
    size: int


@dataclass(slots=True)
class RejectedFile:
    name: str
    reason: str


class UploadRejectedError(ValueError):
    """No uploaded file can be handed to the importer."""

    def __init__(self, rejected: list[RejectedFile]) -> None:
        super().__init__("no uploaded file could be imported")
        self.rejected = rejected


def staging_directory(project_id: str) -> Path:
    """The project's upload folder. The project id is server-minted, but is checked anyway."""
    if not _SAFE_PROJECT_ID.fullmatch(project_id):
        raise ValueError("project id is not a safe directory name")
    return data_root() / "uploads" / project_id


def safe_filename(raw: str) -> str | None:
    """A display name that is also a safe leaf name, or ``None`` when nothing usable is left."""
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_NAME.sub("_", name).strip().strip(".")
    if not name or name in {".", ".."}:
        return None
    if len(name) > MAX_NAME_CHARS:
        suffix = Path(name).suffix[:16]
        name = name[: MAX_NAME_CHARS - len(suffix)] + suffix
    return name


async def stage_uploads(
    project_id: str, uploads: list[UploadFile]
) -> tuple[list[StagedFile], list[RejectedFile]]:
    """Write every acceptable upload and report the rest with a stable reason token.

    Files are written to a temporary sibling and moved into place, so an interrupted request
    never leaves a half file that the next import would read.
    """
    directory = staging_directory(project_id)
    directory.mkdir(parents=True, exist_ok=True)

    staged: list[StagedFile] = []
    rejected: list[RejectedFile] = []
    used: set[str] = set()

    for upload in uploads:
        original = upload.filename or ""
        try:
            name = safe_filename(original)
            if name is None:
                rejected.append(RejectedFile(original or "(unnamed)", "invalid_name"))
                continue
            if Path(name).suffix.lower() not in TEXT_EXTENSIONS:
                rejected.append(RejectedFile(name, "unsupported_extension"))
                continue
            name = _unique_name(name, used)
            staged_file, reason = await _write_upload(upload, directory, name)
            if staged_file is None:
                rejected.append(RejectedFile(name, reason))
                continue
            staged.append(staged_file)
        finally:
            await upload.close()

    return staged, rejected


async def _write_upload(
    upload: UploadFile, directory: Path, name: str
) -> tuple[StagedFile | None, str]:
    path = directory / name
    temporary = directory / f".{name}.{uuid.uuid4().hex}.part"
    suffix = Path(name).suffix.lower()
    size = 0
    try:
        with temporary.open("wb") as handle:
            first_chunk = True
            while chunk := await upload.read(_READ_CHUNK_BYTES):
                if first_chunk and suffix != ".pdf":
                    if b"\x00" in chunk[:BINARY_PROBE_BYTES]:
                        return None, "binary_content"
                    first_chunk = False
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    return None, "too_large"
                handle.write(chunk)
        if size == 0:
            return None, "empty_file"
        os.replace(temporary, path)
        return StagedFile(name=name, path=path, size=size), ""
    except OSError:
        return None, "unreadable"
    finally:
        temporary.unlink(missing_ok=True)


def _unique_name(name: str, used: set[str]) -> str:
    """Disambiguate two files of the same request; a later upload replaces an older file."""
    candidate = name
    stem, suffix = os.path.splitext(name)
    index = 1
    while candidate in used:
        candidate = f"{stem}-{index}{suffix}"
        index += 1
    used.add(candidate)
    return candidate
