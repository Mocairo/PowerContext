import os
import re
from pathlib import Path

import pytest
import yaml

from labrelay.domain.models import Asset, Chunk
from labrelay.importers import assets as assets_module
from labrelay.importers.assets import (
    EXCLUDED_DIRS,
    MAX_FILE_BYTES,
    TEXT_EXTENSIONS,
    build_asset,
    chunk_asset,
    classify_asset,
    import_directory,
    iter_importable_files,
    scan_directory,
    skip_reason,
)
from labrelay.importers.pdf import extract_pdf_chunks

SECURITY_CONFIG = Path(__file__).resolve().parents[3] / "configs" / "security.yaml"
# Every value ``skip_reason`` may return; reasons are single tokens, never a token
# carrying the OS message.
KNOWN_REASON_TOKENS = {
    "directory",
    "excluded_directory",
    "symlink",
    "unsupported_extension",
    "too_large",
    "binary_content",
    "unreadable",
    "build_failed",
    "chunk_failed",
}


def _file_import_config() -> dict[str, object]:
    config = yaml.safe_load(SECURITY_CONFIG.read_text(encoding="utf-8"))
    return dict(config["file_import"])


def test_classify_recognizes_code_config_and_notes(tmp_path: Path) -> None:
    code = tmp_path / "train.py"
    code.write_text("def train():\n    return 1\n", encoding="utf-8")
    config = tmp_path / "baseline.yaml"
    config.write_text("learning_rate: 0.001\n", encoding="utf-8")
    note = tmp_path / "handoff.md"
    note.write_text("# next\nrun lr sweep\n", encoding="utf-8")

    assert classify_asset(code)[1] == "code"
    assert classify_asset(config)[1] == "config"
    assert classify_asset(note)[1] == "note"


def test_classify_applies_suffix_rules_before_substring_keywords(tmp_path: Path) -> None:
    """A name merely containing "server" must not turn a paper or a note into ops."""
    assert classify_asset(tmp_path / "observers.pdf")[1] == "paper"
    assert classify_asset(tmp_path / "observer.md")[1] == "note"
    assert classify_asset(tmp_path / "server.py")[1] == "code"
    assert classify_asset(tmp_path / "server_runbook.md")[1] == "operations"
    assert classify_asset(tmp_path / "resources.md")[1] == "operations"
    assert classify_asset(tmp_path / "experiment_results.csv")[1] == "experiment"


def test_iter_importable_files_skips_excluded_and_binary(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    keep = tmp_path / "README.md"
    keep.write_text("# project", encoding="utf-8")
    binary = tmp_path / "weights.bin"
    binary.write_bytes(b"\x00\x01\x02")
    sneaky = tmp_path / "notes.md"
    sneaky.write_bytes(b"# notes\x00\xff\xfe\x00garbage")

    files = list(iter_importable_files(tmp_path))
    assert files == [keep]


def test_iter_importable_files_accepts_every_allowed_extension(tmp_path: Path) -> None:
    expected: list[Path] = []
    for suffix in sorted(TEXT_EXTENSIONS):
        path = tmp_path / f"sample{suffix}"
        path.write_bytes(b"%PDF-1.4 page text" if suffix == ".pdf" else b"plain text\n")
        expected.append(path)

    assert sorted(iter_importable_files(tmp_path)) == sorted(expected)


def test_import_constants_match_security_yaml() -> None:
    file_import = _file_import_config()

    assert EXCLUDED_DIRS == set(file_import["exclude_directories"])  # type: ignore[arg-type]
    assert TEXT_EXTENSIONS == set(file_import["allowed_text_extensions"])  # type: ignore[arg-type]
    assert ".pdf" in file_import["allowed_text_extensions"]
    assert MAX_FILE_BYTES == file_import["max_file_bytes"]
    assert file_import["skip_binary_files"] is True


def test_asset_with_secret_is_restricted(tmp_path: Path) -> None:
    secret = tmp_path / "config.yaml"
    secret.write_text("api_key: super-secret-value\n", encoding="utf-8")
    asset = build_asset("project-1", secret)
    assert asset.status == "restricted"


# --- discovery beyond the brief's three cases --------------------------------


def test_iter_importable_files_skips_excluded_dirs_oversized_and_unsupported(
    tmp_path: Path,
) -> None:
    leak = tmp_path / ".venv" / "site-packages" / "leak.py"
    leak.parent.mkdir(parents=True)
    leak.write_text("api_key = 'leaked-value'\n", encoding="utf-8")

    huge = tmp_path / "huge.md"
    huge.write_text("# log\n" + ("x" * 40 + "\n") * 30_000, encoding="utf-8")

    (tmp_path / "figure.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    wanted = tmp_path / "notes.md"
    wanted.write_text("# ok\n", encoding="utf-8")

    assert list(iter_importable_files(tmp_path)) == [wanted]


def test_build_asset_is_deterministic_and_carries_checksum(tmp_path: Path) -> None:
    note = tmp_path / "handoff.md"
    note.write_text("# next\nrun lr sweep\n", encoding="utf-8")

    first = build_asset("project-1", note)
    second = build_asset("project-1", note)

    assert first == second
    assert first.id.startswith("asset-")
    assert first.project_id == "project-1"
    assert first.title == "handoff.md"
    assert first.kind == "note"
    assert first.status == "active"
    assert len(first.checksum) == 64


def test_build_asset_identity_is_scoped_to_project_and_resolved_path(tmp_path: Path) -> None:
    """Two projects handoff-ing the same folder must not re-own each other's assets."""
    note = tmp_path / "handoff.md"
    note.write_text("# next\nrun lr sweep\n", encoding="utf-8")

    mine = build_asset("project-1", note)
    theirs = build_asset("project-2", note)
    same = build_asset("project-1", note.resolve())

    assert mine.id != theirs.id
    assert mine.checksum == theirs.checksum == same.checksum
    assert mine.id == same.id
    assert mine.uri == str(note.resolve())


# --- chunking keeps citation-bearing locators --------------------------------


def test_chunk_asset_uses_markdown_sections_for_notes(tmp_path: Path) -> None:
    note = tmp_path / "handoff.md"
    note.write_text("# setup\ninstall uv\n\n# train\nrun train.py\n", encoding="utf-8")

    chunks = chunk_asset("asset-1", note)

    assert [chunk.content for chunk in chunks] == ["# setup\ninstall uv", "# train\nrun train.py"]
    assert [chunk.locator for chunk in chunks] == [{"section": "setup"}, {"section": "train"}]
    assert [chunk.ordinal for chunk in chunks] == [0, 1]
    assert [chunk.asset_id for chunk in chunks] == ["asset-1", "asset-1"]
    assert all(chunk.token_count > 0 for chunk in chunks)


def test_chunk_asset_keeps_leading_text_before_the_first_heading(tmp_path: Path) -> None:
    note = tmp_path / "handoff.md"
    note.write_text("preamble line\n\n# setup\ninstall uv\n", encoding="utf-8")

    chunks = chunk_asset("asset-1", note)

    assert [chunk.content for chunk in chunks] == ["preamble line", "# setup\ninstall uv"]
    assert [chunk.locator for chunk in chunks] == [{"section": "document"}, {"section": "setup"}]


def test_chunk_asset_uses_line_ranges_for_code(tmp_path: Path) -> None:
    code = tmp_path / "train.py"
    code.write_text("\n".join(f"line_{number}" for number in range(1, 201)), encoding="utf-8")

    chunks = chunk_asset("asset-1", code)

    assert [chunk.id for chunk in chunks] == [
        "asset-1:lines:1-80",
        "asset-1:lines:81-160",
        "asset-1:lines:161-200",
    ]
    assert [chunk.locator for chunk in chunks] == [
        {"line_start": 1, "line_end": 80},
        {"line_start": 81, "line_end": 160},
        {"line_start": 161, "line_end": 200},
    ]
    assert [chunk.ordinal for chunk in chunks] == [0, 1, 2]
    assert chunks[0].content.startswith("line_1\n")
    assert chunks[-1].content.endswith("line_200")


def test_chunk_asset_redacts_credentials_before_indexing(tmp_path: Path) -> None:
    code = tmp_path / "train.py"
    code.write_text(
        "api_key=super-secret-value\n"
        "password=another-secret-value\n"
        "def train():\n    return 1\n",
        encoding="utf-8",
    )

    content = "\n".join(chunk.content for chunk in chunk_asset("asset-1", code))

    assert "super-secret-value" not in content
    assert "another-secret-value" not in content
    assert "api_key=[MASK]" in content
    assert "password=[MASK]" in content


# --- PDF extraction ----------------------------------------------------------


def _pdf_bytes(page_texts: list[str | None]) -> bytes:
    """Build a minimal text-only PDF; ``None`` stands for an image-only page."""
    page_count = len(page_texts)
    first_page_id = 3
    font_id = first_page_id + 2 * page_count
    kids = " ".join(f"{first_page_id + 2 * index} 0 R" for index in range(page_count))

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode("ascii"),
    ]
    for index, page_text in enumerate(page_texts):
        page_id = first_page_id + 2 * index
        content_id = page_id + 1
        stream = (
            b""
            if page_text is None
            else f"BT /F1 12 Tf 20 100 Td ({page_text}) Tj ET".encode("ascii")
        )
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents {content_id} 0 R "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>"
            ).encode("ascii")
        )
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

    cross_reference = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{cross_reference}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


def _write_pdf(path: Path, page_texts: list[str | None]) -> Path:
    path.write_bytes(_pdf_bytes(page_texts))
    return path


def test_extract_pdf_chunks_paginates_with_page_locators(tmp_path: Path) -> None:
    pdf = _write_pdf(tmp_path / "paper.pdf", ["qoder results table", "ablation section"])

    chunks = extract_pdf_chunks(pdf, "asset-9")

    assert [chunk.locator for chunk in chunks] == [{"page": 1}, {"page": 2}]
    assert [chunk.id for chunk in chunks] == ["asset-9:page:1", "asset-9:page:2"]
    assert [chunk.ordinal for chunk in chunks] == [0, 1]
    assert "qoder" in chunks[0].content
    assert "ablation" in chunks[1].content


def test_extract_pdf_chunks_redacts_credentials_in_page_text(tmp_path: Path) -> None:
    pdf = _write_pdf(tmp_path / "leak.pdf", ["api_key=super-secret-value", "clean page"])

    chunks = extract_pdf_chunks(pdf, "asset-9")

    assert "super-secret-value" not in chunks[0].content
    assert chunks[0].content == "api_key=[MASK]"
    assert chunks[1].content == "clean page"


def test_build_asset_restricts_a_pdf_whose_page_text_carries_a_credential(
    tmp_path: Path,
) -> None:
    leaky = _write_pdf(tmp_path / "leak.pdf", ["api_key=super-secret-value"])
    clean = _write_pdf(tmp_path / "paper.pdf", ["ablation section"])

    assert build_asset("project-1", leaky).status == "restricted"
    assert build_asset("project-1", clean).status == "active"


def test_extract_pdf_chunks_without_text_reports_ocr_need(tmp_path: Path) -> None:
    scanned = _write_pdf(tmp_path / "scan.pdf", [None])

    with pytest.raises(ValueError, match="OCR is required"):
        extract_pdf_chunks(scanned, "asset-9")


def test_extract_pdf_chunks_rejects_unreadable_file(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"%PDF-1.4 not really a pdf")

    with pytest.raises(ValueError, match="could not be read"):
        extract_pdf_chunks(corrupt, "asset-9")


# --- directory import --------------------------------------------------------


class FakeRepository:
    def __init__(self) -> None:
        self.assets: dict[str, Asset] = {}

    async def save_asset(self, asset: Asset) -> None:
        self.assets[asset.id] = asset


class FakeSearchEngine:
    def __init__(self) -> None:
        self.chunks: dict[str, Chunk] = {}
        self.asset_ids: list[str] = []
        self.calls = 0

    async def upsert_chunks(self, chunks: list[Chunk], assets: list[Asset]) -> None:
        self.calls += 1
        self.asset_ids = [asset.id for asset in assets]
        for chunk in chunks:
            self.chunks[chunk.id] = chunk


def _sample_project(root: Path) -> Path:
    (root / "handoff.md").write_text("# next\nrun lr sweep\n", encoding="utf-8")
    (root / "train.py").write_text("def train():\n    return 1\n", encoding="utf-8")
    (root / "config.yaml").write_text("api_key: super-secret-value\n", encoding="utf-8")
    _write_pdf(root / "paper.pdf", ["citation evidence"])
    return root


def _make_unreadable(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Make ``path`` fail to stat, the way a locked or racy file does.

    ``chmod`` cannot express "unreadable" portably (Windows keeps owner read access),
    so the stat call itself is failed with a fixed, non-localised OS message.
    """
    real_stat = Path.stat

    def flaky_stat(self: Path, **kwargs: object) -> os.stat_result:
        if self.name == path.name:
            raise PermissionError(13, "permission denied")
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)


async def test_import_directory_persists_assets_and_indexes_chunks(tmp_path: Path) -> None:
    source = _sample_project(tmp_path)
    restricted = build_asset("project-1", source / "config.yaml")
    repository = FakeRepository()
    search_engine = FakeSearchEngine()

    result = await import_directory(repository, search_engine, "project-1", source)

    assert result == {
        "assets": 4,
        "chunks": 3,
        "failed": 0,
        "skipped": 0,
        "restricted": 1,
        "restricted_reasons": {restricted.id: ["api_secret"]},
    }
    assert len(repository.assets) == 4
    assert search_engine.calls == 1
    assert len(search_engine.chunks) == result["chunks"]
    assert all(chunk.asset_id in repository.assets for chunk in search_engine.chunks.values())
    assert all(
        "super-secret-value" not in chunk.content for chunk in search_engine.chunks.values()
    )


async def test_import_directory_does_not_index_restricted_assets(tmp_path: Path) -> None:
    source = _sample_project(tmp_path)
    restricted = build_asset("project-1", source / "config.yaml")
    assert restricted.status == "restricted"

    repository = FakeRepository()
    search_engine = FakeSearchEngine()
    result = await import_directory(repository, search_engine, "project-1", source)

    assert repository.assets[restricted.id] == restricted
    assert result["restricted"] == 1
    assert result["restricted_reasons"] == {restricted.id: ["api_secret"]}
    assert restricted.id not in search_engine.asset_ids
    assert sorted(search_engine.asset_ids) == sorted(
        asset.id for asset in repository.assets.values() if asset.status == "active"
    )
    assert all(chunk.asset_id != restricted.id for chunk in search_engine.chunks.values())


async def test_import_directory_is_idempotent(tmp_path: Path) -> None:
    source = _sample_project(tmp_path)
    repository = FakeRepository()
    search_engine = FakeSearchEngine()

    first = await import_directory(repository, search_engine, "project-1", source)
    asset_ids = sorted(repository.assets)
    chunk_ids = sorted(search_engine.chunks)
    second = await import_directory(repository, search_engine, "project-1", source)

    assert first == second
    assert sorted(repository.assets) == asset_ids
    assert sorted(search_engine.chunks) == chunk_ids


async def test_import_directory_keeps_projects_that_share_a_path_separate(tmp_path: Path) -> None:
    source = _sample_project(tmp_path)
    repository = FakeRepository()
    search_engine = FakeSearchEngine()

    await import_directory(repository, search_engine, "project-1", source)
    await import_directory(repository, search_engine, "project-2", source)

    assert len(repository.assets) == 8
    assert len(search_engine.chunks) == 6


async def test_import_directory_requires_an_existing_directory(tmp_path: Path) -> None:
    plain_file = tmp_path / "README.md"
    plain_file.write_text("# ok\n", encoding="utf-8")

    for root in (tmp_path / "does-not-exist", plain_file):
        with pytest.raises(ValueError, match="not an existing directory"):
            await import_directory(FakeRepository(), FakeSearchEngine(), "project-1", root)


async def test_import_directory_survives_unreadable_files_after_partial_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = _sample_project(tmp_path)
    original_chunk_asset = assets_module.chunk_asset

    def flaky_chunk_asset(asset_id: str, path: Path) -> list[Chunk]:
        if path.name == "train.py":
            raise OSError("permission denied")
        return original_chunk_asset(asset_id, path)

    monkeypatch.setattr(assets_module, "chunk_asset", flaky_chunk_asset)
    repository = FakeRepository()
    search_engine = FakeSearchEngine()

    with caplog.at_level("WARNING", logger="labrelay.importers.assets"):
        result = await import_directory(repository, search_engine, "project-1", source)

    assert result == {
        "assets": 3,
        "chunks": 2,
        "failed": 1,
        "skipped": 0,
        "restricted": 1,
        "restricted_reasons": {build_asset("project-1", source / "config.yaml").id: ["api_secret"]},
    }
    failed = [asset for asset in repository.assets.values() if asset.status == "failed"]
    assert [asset.title for asset in failed] == ["train.py"]
    assert search_engine.calls == 1
    assert "permission denied" in caplog.text


async def test_import_directory_counts_and_logs_skipped_files(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    keep = tmp_path / "README.md"
    keep.write_text("# ok\nkeep me\n", encoding="utf-8")
    (tmp_path / "wandb").mkdir()
    (tmp_path / "wandb" / "run.md").write_text("# offline run\n", encoding="utf-8")
    (tmp_path / "figure.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "raw.md").write_bytes(b"# raw\x00\x01\x02")
    (tmp_path / "huge.md").write_text("# log\n" + ("x" * 40 + "\n") * 30_000, encoding="utf-8")

    repository = FakeRepository()
    search_engine = FakeSearchEngine()

    with caplog.at_level("INFO", logger="labrelay.importers.assets"):
        result = await import_directory(repository, search_engine, "project-1", tmp_path)

    assert result == {
        "assets": 1,
        "chunks": 1,
        "failed": 0,
        "skipped": 4,
        "restricted": 0,
        "restricted_reasons": {},
    }
    assert sorted(search_engine.asset_ids) == [build_asset("project-1", keep).id]
    for reason in ("excluded_directory", "unsupported_extension", "binary_content", "too_large"):
        assert reason in caplog.text
        assert f"reason={reason}" in caplog.text
    # Reasons stay single tokens, so nothing dynamic leaks into the reason field.
    assert set(re.findall(r"reason=(\S+)", caplog.text)) <= KNOWN_REASON_TOKENS


async def test_import_directory_marks_scanned_pdf_failed_without_indexing(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_pdf(tmp_path / "scan.pdf", [None])
    note = tmp_path / "README.md"
    note.write_text("# ok\nkeep me\n", encoding="utf-8")
    repository = FakeRepository()
    search_engine = FakeSearchEngine()

    with caplog.at_level("WARNING", logger="labrelay.importers.assets"):
        result = await import_directory(repository, search_engine, "project-1", tmp_path)

    assert result == {
        "assets": 1,
        "chunks": 1,
        "failed": 1,
        "skipped": 0,
        "restricted": 0,
        "restricted_reasons": {},
    }
    failed = [asset for asset in repository.assets.values() if asset.status == "failed"]
    assert [asset.title for asset in failed] == ["scan.pdf"]
    assert failed[0].id not in search_engine.asset_ids
    assert [chunk.asset_id for chunk in search_engine.chunks.values()] == [
        build_asset("project-1", note).id
    ]
    assert "OCR is required" in caplog.text


# --- restricted / unreadable operator visibility --------------------------------


def test_unreadable_skip_reason_stays_a_single_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``unreadable`` carries no OS text; the detail is a separate field."""
    locked = tmp_path / "locked.md"
    locked.write_text("evidence\n", encoding="utf-8")
    _make_unreadable(monkeypatch, locked)

    assert skip_reason(locked, tmp_path) == "unreadable"
    assert "unreadable: permission denied" not in str(skip_reason(locked, tmp_path))
    importable, skipped = scan_directory(tmp_path)
    assert importable == []
    assert skipped == [(locked, "unreadable", "permission denied")]


def test_skip_reason_still_separates_directories_from_files(tmp_path: Path) -> None:
    """The single-stat rewrite must not change what counts as a candidate file."""
    (tmp_path / "sub").mkdir()
    note = tmp_path / "notes.md"
    note.write_text("# ok\nkeep me\n", encoding="utf-8")

    assert skip_reason(tmp_path / "sub", tmp_path) == "directory"
    assert skip_reason(note, tmp_path) is None
    assert scan_directory(tmp_path) == ([note], [])


async def test_import_directory_reports_restricted_and_unreadable_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One run reports both the blocked evidence and the file it could not read."""
    readme = tmp_path / "README.md"
    readme.write_text(
        "# handoff\napi_key: super-secret-value\npassword: another-secret-value\n",
        encoding="utf-8",
    )
    clean = tmp_path / "notes.md"
    clean.write_text("# ok\nkeep me\n", encoding="utf-8")
    locked = tmp_path / "locked.md"
    locked.write_text("unreachable evidence\n", encoding="utf-8")
    _make_unreadable(monkeypatch, locked)

    repository = FakeRepository()
    search_engine = FakeSearchEngine()
    with caplog.at_level("INFO", logger="labrelay.importers.assets"):
        result = await import_directory(repository, search_engine, "project-1", tmp_path)

    restricted_id = build_asset("project-1", readme).id
    assert result == {
        "assets": 2,
        "chunks": 1,
        "failed": 0,
        "skipped": 1,
        "restricted": 1,
        "restricted_reasons": {restricted_id: ["api_secret", "password_assignment"]},
    }
    # Count and detail never disagree.
    assert len(result["restricted_reasons"]) == result["restricted"]
    assert all(len(reasons) > 0 for reasons in result["restricted_reasons"].values())
    assert all(reasons == sorted(reasons) for reasons in result["restricted_reasons"].values())

    # Restricted evidence is stored and stays out of the index.
    assert repository.assets[restricted_id].status == "restricted"
    assert restricted_id not in search_engine.asset_ids
    assert sorted(search_engine.asset_ids) == [build_asset("project-1", clean).id]
    assert [chunk.asset_id for chunk in search_engine.chunks.values()] == [
        build_asset("project-1", clean).id
    ]

    messages = [record.getMessage() for record in caplog.records]
    restricted_lines = [m for m in messages if "operation=import_restricted_asset" in m]
    assert len(restricted_lines) == 1
    assert f"asset_id={restricted_id}" in restricted_lines[0]
    assert "findings=api_secret,password_assignment" in restricted_lines[0]
    assert "indexed=false" in restricted_lines[0]

    skip_lines = [m for m in messages if "reason=unreadable" in m]
    assert len(skip_lines) == 1
    assert "detail=permission denied" in skip_lines[0]
    assert set(re.findall(r"reason=(\S+)", caplog.text)) <= KNOWN_REASON_TOKENS

    # Neither the blocked content nor the secrets it held leave the process.
    assert "super-secret-value" not in caplog.text
    assert "another-secret-value" not in caplog.text
    assert "unreachable evidence" not in caplog.text
    assert "super-secret-value" not in repr(result)
