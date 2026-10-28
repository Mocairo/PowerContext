from pathlib import Path

import pytest

from labrelay.config import load_app_config

PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Environment variables consulted by load_app_config for the values asserted
# below. Clearing them keeps every test hermetic regardless of the shell the
# suite happens to run in.
_CONFIG_ENV_VARS = (
    "LABRELAY_API_PORT",
    "LABRELAY_API_BIND_ADDRESS",
    "EMBEDDING_DIMENSIONS",
    "SEEKDB_PORT",
)


@pytest.fixture(autouse=True)
def _isolate_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _CONFIG_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _write_configs(root: Path, **files: str) -> Path:
    configs_dir = root / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (configs_dir / f"{name}.yaml").write_text(text, encoding="utf-8")
    return root


# Finding 1: a present top-level section whose value is null must not crash.
def test_null_sections_fall_back_to_defaults(tmp_path: Path) -> None:
    root = _write_configs(
        tmp_path,
        app="server:\n",
        retrieval="retrieval:\n",
        planner="planning:\n",
    )
    config = load_app_config(root)
    assert config.server.port == 8010
    assert config.retrieval.fusion.output_limit == 12
    assert config.planner.environment_before_experiments is True


def test_null_nested_section_falls_back(tmp_path: Path) -> None:
    root = _write_configs(tmp_path, retrieval="retrieval:\n  fusion:\n")
    config = load_app_config(root)
    assert config.retrieval.fusion.output_limit == 12
    assert config.retrieval.fusion.full_text_weight == 0.45


# Finding 1: non-mapping YAML must fail with a clear, actionable error.
def test_non_mapping_yaml_raises_clear_error(tmp_path: Path) -> None:
    root = _write_configs(tmp_path, app="- this is a list, not a mapping\n")
    with pytest.raises(ValueError, match="app.yaml"):
        load_app_config(root)


# Finding 2: the env override must actually beat configs/app.yaml (which pins 8010).
def test_env_port_overrides_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LABRELAY_API_PORT", "9099")
    config = load_app_config(PROJECT_ROOT)
    assert config.server.port == 9099


# Finding 3: the advertised bind-address env var must be honored too.
def test_env_bind_address_overrides_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LABRELAY_API_BIND_ADDRESS", "0.0.0.0")
    config = load_app_config(PROJECT_ROOT)
    assert config.server.bind_address == "0.0.0.0"


# Baselines for the two overrides above: without env the YAML values are used,
# proving 9099 / 0.0.0.0 come from the environment rather than coincidence.
def test_yaml_server_values_without_env() -> None:
    config = load_app_config(PROJECT_ROOT)
    assert config.server.port == 8010
    assert config.server.bind_address == "127.0.0.1"


# Finding 2: YAML must actually beat the Pydantic defaults (default limit 12,
# default flag True); these tmp configs carry different values.
def test_yaml_values_override_defaults(tmp_path: Path) -> None:
    root = _write_configs(
        tmp_path,
        retrieval="retrieval:\n  fusion:\n    output_limit: 7\n",
        planner="planning:\n  constraints:\n    environment_before_experiments: false\n",
    )
    config = load_app_config(root)
    assert config.retrieval.fusion.output_limit == 7
    assert config.planner.environment_before_experiments is False


# Finding 2 (follow-up): the other planner flag must be YAML-loadable too, not
# just defaulted.  ``enforce_stage_order`` defaults to True, so a YAML file that
# sets it false proves the value comes from the file rather than the default.
def test_yaml_enforce_stage_order_false_beats_default(tmp_path: Path) -> None:
    root = _write_configs(
        tmp_path,
        planner="planning:\n  constraints:\n    enforce_stage_order: false\n",
    )
    config = load_app_config(root)
    assert config.planner.enforce_stage_order is False


def test_enforce_stage_order_defaults_true_when_yaml_omits_it(tmp_path: Path) -> None:
    root = _write_configs(
        tmp_path,
        planner="planning:\n  constraints:\n    environment_before_experiments: true\n",
    )
    config = load_app_config(root)
    assert config.planner.enforce_stage_order is True


# Finding 2: positive embedding dimensions must be parsed, not just the absent case.
def test_positive_embedding_dimensions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "1024")
    config = load_app_config(PROJECT_ROOT)
    assert config.embedding.dimensions == 1024


# Existing behavior, kept: absent dimensions stays None.
def test_missing_embedding_dimensions_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EMBEDDING_DIMENSIONS", raising=False)
    config = load_app_config(PROJECT_ROOT)
    assert config.embedding.dimensions is None
