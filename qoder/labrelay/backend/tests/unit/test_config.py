from pathlib import Path

from labrelay.config import load_app_config

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_load_app_config_reads_yaml_and_env(monkeypatch) -> None:
    monkeypatch.setenv("LABRELAY_API_PORT", "8010")
    config = load_app_config(PROJECT_ROOT)
    assert config.server.port == 8010
    assert config.retrieval.fusion.output_limit == 12
    assert config.planner.environment_before_experiments is True


def test_load_app_config_requires_embedding_dimensions(monkeypatch) -> None:
    monkeypatch.delenv("EMBEDDING_DIMENSIONS", raising=False)
    config = load_app_config(PROJECT_ROOT)
    assert config.embedding.dimensions is None
