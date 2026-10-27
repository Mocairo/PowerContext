"""Additional contract tests for CSV row evidence, independent of the public sample."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import random
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

from powercontext.sources import (
    SourceDefinitionRegistry,
    TextEvidence,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.csv_rows import (
    CSV_ROW_SOURCE_DEFINITION,
    CSVDocumentInput,
    CSVRowInput,
    CSVRowSource,
    csv_row_source_id,
    parse_csv_document,
)
from powercontext.sources.models import SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


def _parse(content: bytes, *, document_id: str = "document", primary_key: str = "id") -> tuple[CSVRowInput, ...]:
    return parse_csv_document(CSVDocumentInput(document_id=document_id, primary_key=primary_key, content=content))


def _encode(columns: tuple[str, ...], records: list[tuple[str, ...]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(columns)
    writer.writerows(records)
    return output.getvalue().encode("utf-8")


@pytest.mark.parametrize(
    "content",
    [
        b"\n",
        b"\n" + b"id,value\n1,ok\n",
        b"\xef\xbb\xbf",
        b'""\n',
        b"id,\n",
        b"id,id\n",
        b"id,\t\n",
        "id,\u2003\n".encode(),
        b'id\n""\n',
        b"id\n \n",
        b"id\n\t\n",
        b"id,value\n,\n",
        b"id,value\n1,ok,extra\n",
        b"id,value\n1\n",
        b'id,value\n1,"bad"tail\n',
        b'id,value\n1,"bad" \n',
        b'id,value\n1,"unterminated\r\n',
        b"id,value\n1,\xc0\xaf\n",
        b"id,value\n1,\xed\xa0\x80\n",
        b"id,value\n1,\xe4\xb8\n",
    ],
)
def test_invalid_documents_fail_immediately(content: bytes) -> None:
    with pytest.raises(ValueError):
        _parse(content)


@pytest.mark.parametrize("document_id", ["", " ", "\tdoc", "doc\n", "\u2003doc", "doc\u2003"])
def test_document_identity_validation_also_applies_to_header_only_input(document_id: str) -> None:
    with pytest.raises(ValueError):
        _parse(b"id,value\n", document_id=document_id)


@pytest.mark.parametrize("primary_key", ["", " id", "id ", "ID", "missing"])
def test_primary_key_is_an_exact_header_match(primary_key: str) -> None:
    with pytest.raises(ValueError):
        _parse(b"id,value\n1,ok\n", primary_key=primary_key)


@pytest.mark.parametrize("terminator", ["\r", "\n", "\r\n"])
def test_empty_physical_records_and_embedded_newlines(terminator: str) -> None:
    content = terminator.join(["id,value", "", '1,"first\r\nsecond\rthird\nfourth"', "", "2,", ""])
    rows = _parse(content.encode())
    assert isinstance(rows, tuple)
    assert [row.values for row in rows] == [("1", "first\r\nsecond\rthird\nfourth"), ("2", "")]
    assert _parse(("id,value" + terminator * 3).encode()) == ()


def test_bom_is_removed_only_at_start_of_file() -> None:
    content = "\ufeffid,\ufeffvalue\n\ufeff001,\ufeffpayload\n".encode()
    row = _parse(content)[0]
    assert row.columns == ("id", "\ufeffvalue")
    assert row.values == ("\ufeff001", "\ufeffpayload")
    assert _parse(b"\xef\xbb\xbfid\n") == ()


@pytest.mark.parametrize("document_id", ["x" * 1000, "\ud800"])
def test_header_only_document_does_not_generate_a_row_identity(document_id: str) -> None:
    assert _parse(b"id,value\n\n", document_id=document_id) == ()
    with pytest.raises(ValueError):
        _parse(b"id,value\n1,data\n", document_id=document_id)


def test_standard_reader_field_size_boundary() -> None:
    value = b"x" * csv.field_size_limit()
    assert _parse(b"id,value\n1," + value)[0].values == ("1", value.decode())
    with pytest.raises(ValueError):
        _parse(b"id,value\n1," + value + b"x")


@pytest.mark.parametrize(
    "record",
    ['1,unquoted"quote', '1, "not quoted"', '1,"""quote"""', '1,"backslash\\value"', '1,"nul\0value"'],
)
def test_fixed_strict_reader_semantics_accept_legal_unusual_fields(record: str) -> None:
    text = "id,value\n" + record
    expected = tuple(csv.reader(io.StringIO(text, newline=""), strict=True))[1]
    assert _parse(text.encode())[0].values == tuple(expected)


@pytest.mark.parametrize("suffix", [b"1,duplicate\n", b"2\n", b'2,"bad"tail\n', b"2,\xff\n"])
def test_late_error_does_not_return_a_partial_tuple(suffix: bytes) -> None:
    with pytest.raises(ValueError):
        _parse(b"id,value\n1,valid\n" + suffix)


def test_whitespace_and_lookalike_columns_and_values_remain_distinct() -> None:
    columns = (" id ", "id", "ID", "a", "a ", "数字")
    records = [(key, "001", "false", "null", "", "1.00") for key in ("key", " key", "key ", " key ")]
    rows = _parse(_encode(columns, records), primary_key=" id ")
    assert [row.columns for row in rows] == [columns] * len(records)
    assert [row.values for row in rows] == records
    assert [row.primary_key_value for row in rows] == [record[0] for record in records]
    assert len({csv_row_source_id(row.document_id, row.primary_key_value) for row in rows}) == len(rows)


@pytest.mark.parametrize("key", ["a" * 250, "中" * 27 + "a" * 7])
def test_source_identity_exact_encoded_length_limit(key: str) -> None:
    assert len(csv_row_source_id("d", key)) == 256
    with pytest.raises(ValueError):
        csv_row_source_id("d", key + "a")
    assert _parse(_encode(("id",), [(key,)]), document_id="d")[0].primary_key_value == key
    with pytest.raises(ValueError):
        _parse(_encode(("id",), [("ok",), (key + "a",)]), document_id="d")


def test_identity_encoding_has_no_component_or_normalization_collisions() -> None:
    pairs = [("a:b", "c"), ("a", "b:c"), ("a%3Ab", "c"), ("a/b", "x y"), ("a b", "x/y")]
    pairs += [("记录:文件/一", key) for key in ("é", "e\u0301", "A", "a", " key ", "%", "\0")]
    identities = [csv_row_source_id(document, key) for document, key in pairs]
    assert identities == [f"csv:{quote(document, safe='')}:{quote(key, safe='')}" for document, key in pairs]
    assert len(set(identities)) == len(pairs)


def test_seeded_random_csv_roundtrip_and_complete_projection() -> None:
    rng = random.Random(2702026)  # noqa: S311 - reproducible test data, not security-sensitive randomness
    columns = ("id", "空字段", "value", "quoted", " multiline ", "numeric")
    alphabet = ' abAZ09,\r\n\t"\\中é😀\ufeff\0'
    records = [
        (f" {index:06d} ", "", *("".join(rng.choices(alphabet, k=rng.randrange(45))) for _ in range(3)), "0001.00")
        for index in range(2048)
    ]
    rows = _parse(_encode(columns, records), document_id="随机:表/数据")
    assert [row.values for row in rows] == records
    assert all(row.columns == columns for row in rows)
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    assert CSV_ROW_SOURCE_DEFINITION.name == "csv-row"
    assert CSV_ROW_SOURCE_DEFINITION.version == "1"
    assert CSV_ROW_SOURCE_DEFINITION.input_class is CSVRowInput
    assert CSV_ROW_SOURCE_DEFINITION.source_class is CSVRowSource
    manifest = manifest_for_definition(CSV_ROW_SOURCE_DEFINITION)
    assert manifest.name == "csv-row"

    async def verify() -> None:
        for index in (0, 1, 1023, 2047):
            row = rows[index]
            source = await registry.resolve(row)
            assert source.materialization is SourceMaterialization.CAPTURED
            assert source.source_id == source.name == csv_row_source_id(row.document_id, row.primary_key_value)
            assert await registry.read(CSVRowSource.model_validate_json(source.model_dump_json())) == row
            mapping = dict(zip(columns, records[index], strict=True))
            observation = project_source_for_transport(registry, source)
            evidence = TextEvidence.model_validate(observation.projection(TEXT_EVIDENCE_PROJECTION_KEY))
            assert evidence.source_type == "csv-row"
            assert evidence.source_id == source.source_id
            assert evidence.content == json.dumps(mapping, ensure_ascii=False, separators=(",", ":"))
            assert evidence.metadata == {
                "document_id": row.document_id,
                "primary_key": "id",
                "primary_key_value": row.primary_key_value,
                "columns": list(columns),
                "row": mapping,
            }

    asyncio.run(verify())


_INGEST_IN_NEW_PROCESS = """
import asyncio
import sys
from pathlib import Path
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import BuiltinConfig, SubmitSourceObservation, open_builtin_runtime
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import SourceDefinitionRegistry, manifest_for_definition, project_source_for_transport
from powercontext.sources.csv_rows import CSV_ROW_SOURCE_DEFINITION, CSVDocumentInput, parse_csv_document

async def main():
    database, csv_file, scope_file = map(Path, sys.argv[1:])
    rows = parse_csv_document(CSVDocumentInput(
        document_id='补充:记录/一', primary_key=' id ', content=csv_file.read_bytes()))
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    config = BuiltinConfig(database=SQLiteConfig(url=f'sqlite+aiosqlite:///{database}'))
    async with open_builtin_runtime(config) as runtime:
        scope = await runtime.scopes.create(ScopeDraft(
            title='Supplementary CSV', summary='Independent restart test', idempotency_key='supplementary-csv'))
        await runtime.ingestion.register(manifest_for_definition(CSV_ROW_SOURCE_DEFINITION))
        for row in rows:
            source = await registry.resolve(row)
            await runtime.ingestion.submit(SubmitSourceObservation(
                scope_id=scope.scope_id, observation=project_source_for_transport(registry, source)))
        scope_file.write_text(scope.scope_id, encoding='utf-8')

asyncio.run(main())
"""


def test_supplementary_rows_survive_process_restart_and_precise_citations(tmp_path: Path) -> None:
    from powercontext.builtin.artifacts.memory import MemoryEntryInput
    from powercontext.builtin.artifacts.memory.models import MemoryCitation
    from powercontext.builtin.persistence.sqlite import SQLiteConfig
    from powercontext.builtin.runtime import (
        BuiltinConfig,
        GetMemoryEntryRequest,
        SearchMemoryRequest,
        open_builtin_contexts,
        open_builtin_runtime,
    )

    columns = (" id ", "phrase", "备注", "empty", "numeric")
    records = [
        (" 001 ", "restartalpha", '中文\r\n第二行,"引号"', "", "0001.00"),
        ("001", "restartbeta", "é 😀 / : %", "", "false"),
        ("key:/%", "restartgamma", "first\rsecond\nthird", "", "null"),
    ]
    csv_file, database, scope_file = (tmp_path / name for name in ("input.csv", "rows.db", "scope.txt"))
    csv_file.write_bytes(_encode(columns, records))
    subprocess.run(
        [sys.executable, "-c", _INGEST_IN_NEW_PROCESS, str(database), str(csv_file), str(scope_file)],
        check=True,
        timeout=60,
        capture_output=True,
        text=True,
    )
    scope_id = scope_file.read_text(encoding="utf-8")
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{database}"))
    expected = {
        csv_row_source_id("补充:记录/一", record[0]): dict(zip(columns, record, strict=True)) for record in records
    }

    class FixedPipeline:
        async def extract(self, request, /):
            return tuple(
                MemoryEntryInput(
                    kind="csv-row",
                    text=source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"],
                    sources=(source,),
                )
                for source in request.sources
            )

    async def verify() -> None:
        async with open_builtin_contexts(config) as contexts:
            persisted = await (await contexts.get(scope_id)).sources.journal.entries()
            assert len(persisted) == len(records)
            for entry in persisted:
                evidence = TextEvidence.model_validate(entry.source.projection(TEXT_EVIDENCE_PROJECTION_KEY))
                mapping = expected[entry.source_ref.source_id]
                assert evidence.content == json.dumps(mapping, ensure_ascii=False, separators=(",", ":"))
                assert evidence.metadata == {
                    "document_id": "补充:记录/一",
                    "primary_key": " id ",
                    "primary_key_value": mapping[" id "],
                    "columns": list(columns),
                    "row": mapping,
                }

        async with open_builtin_runtime(config, candidate_pipeline=FixedPipeline()) as runtime:
            memory = runtime.memory.for_scope(scope_id)
            processed = 0
            for _ in range(4):
                result = await memory.flush()
                if not result.processed:
                    break
                processed += result.source_count
            assert processed == len(records)
            for record in records:
                search = await memory.search(SearchMemoryRequest(query=record[1], mode="fts", limit=10))
                expected_id = csv_row_source_id("补充:记录/一", record[0])
                matches = []
                for hit in search.hits:
                    citation = MemoryCitation(
                        memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id
                    )
                    result = await memory.get(GetMemoryEntryRequest(citation=citation))
                    if any(source.source_id == expected_id for source in result.entry.sources):
                        matches.append(result.entry.text)
                assert matches == [json.dumps(expected[expected_id], ensure_ascii=False, separators=(",", ":"))]

    asyncio.run(verify())
