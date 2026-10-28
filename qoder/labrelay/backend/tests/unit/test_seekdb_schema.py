from pathlib import Path


def test_seekdb_schema_contains_core_tables() -> None:
    sql = (Path(__file__).resolve().parents[2] / "src/labrelay/seekdb/schema.sql").read_text(
        encoding="utf-8"
    )
    for table in ("projects", "assets", "assessments", "plans", "tasks", "task_events"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
