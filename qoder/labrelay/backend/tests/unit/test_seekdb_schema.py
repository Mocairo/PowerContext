from pathlib import Path

import pytest

SCHEMA = Path(__file__).resolve().parents[2] / "src/labrelay/seekdb/schema.sql"


def test_seekdb_schema_contains_core_tables() -> None:
    sql = SCHEMA.read_text(encoding="utf-8")
    for table in ("projects", "assets", "assessments", "plans", "tasks", "task_events"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql


@pytest.mark.parametrize(
    ("table", "sequence_key"),
    [("assets", "uq_asset_seq"), ("task_events", "uq_event_seq")],
)
def test_write_order_survives_a_same_timestamp_row(table: str, sequence_key: str) -> None:
    """Both append-ordered tables carry a monotonic sequence, not just a timestamp."""
    block = _table_block(SCHEMA.read_text(encoding="utf-8"), table)

    assert "seq BIGINT NOT NULL AUTO_INCREMENT" in block
    assert f"UNIQUE KEY {sequence_key} (seq)" in block


def test_task_order_is_an_explicit_column() -> None:
    block = _table_block(SCHEMA.read_text(encoding="utf-8"), "tasks")

    assert "position INT NOT NULL DEFAULT 0" in block
    assert "INDEX idx_tasks_plan_position (plan_id, position)" in block


def _table_block(sql: str, table: str) -> str:
    header = f"CREATE TABLE IF NOT EXISTS {table} ("
    start = sql.index(header)
    return sql[start : sql.index(");", start)]
