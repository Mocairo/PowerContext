from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class ServerConfig(BaseModel):
    bind_address: str = "127.0.0.1"
    port: int = 8010


class RetrievalConfig(BaseModel):
    class Fusion(BaseModel):
        full_text_weight: float = 0.45
        vector_weight: float = 0.55
        output_limit: int = 12

    fusion: Fusion = Field(default_factory=Fusion)


class PlannerConfig(BaseModel):
    enforce_stage_order: bool = True
    environment_before_experiments: bool = True
    every_task_requires_citation: bool = True


class EmbeddingConfig(BaseModel):
    dimensions: int | None = None


class LabRelayConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    planner: PlannerConfig = Field(default_factory=PlannerConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    powercontext_url: str = "http://127.0.0.1:8000"
    powercontext_token: str | None = None
    generation_base_url: str | None = None
    generation_api_key: str | None = None
    generation_model: str | None = None
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
    embedding_model: str | None = None
    seekdb_host: str = "127.0.0.1"
    seekdb_port: int = 2881
    seekdb_database: str = "labrelay"
    seekdb_user: str = "root"
    seekdb_password: str = ""
    seekdb_tenant: str = "sys"


def _yaml_value(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(
            f"{path} must contain a key/value mapping at the top level, but parsed as "
            f"{type(loaded).__name__}. Give the file a single top-level key "
            f"(for example 'server:') instead of a list or scalar."
        )
    return loaded


def load_app_config(project_root: Path) -> LabRelayConfig:
    app = _yaml_value(project_root / "configs/app.yaml")
    retrieval = _yaml_value(project_root / "configs/retrieval.yaml")
    planner = _yaml_value(project_root / "configs/planner.yaml")

    server = app.get("server") or {}
    if os.getenv("LABRELAY_API_PORT"):
        server = {**server, "port": int(os.environ["LABRELAY_API_PORT"])}
    if os.getenv("LABRELAY_API_BIND_ADDRESS"):
        server = {**server, "bind_address": os.environ["LABRELAY_API_BIND_ADDRESS"]}

    retrieval_cfg = (retrieval.get("retrieval") or {}).get("fusion") or {}
    planner_cfg = (planner.get("planning") or {}).get("constraints") or {}

    dimensions = os.getenv("EMBEDDING_DIMENSIONS")
    return LabRelayConfig(
        server=ServerConfig(**server),
        retrieval=RetrievalConfig(fusion=RetrievalConfig.Fusion(**retrieval_cfg)),
        planner=PlannerConfig(
            enforce_stage_order=planner_cfg.get("enforce_stage_order", True),
            environment_before_experiments=planner_cfg.get("environment_before_experiments", True),
            every_task_requires_citation=planner_cfg.get("every_task_requires_citation", True),
        ),
        embedding=EmbeddingConfig(
            dimensions=int(dimensions) if dimensions and dimensions.isdigit() else None
        ),
        powercontext_url=os.getenv("POWERCONTEXT_URL", "http://127.0.0.1:8000"),
        powercontext_token=os.getenv("POWERCONTEXT_TOKEN") or None,
        generation_base_url=os.getenv("GENERATION_BASE_URL") or None,
        generation_api_key=os.getenv("GENERATION_API_KEY") or None,
        generation_model=os.getenv("GENERATION_MODEL") or None,
        embedding_base_url=os.getenv("EMBEDDING_BASE_URL") or None,
        embedding_api_key=os.getenv("EMBEDDING_API_KEY") or None,
        embedding_model=os.getenv("EMBEDDING_MODEL") or None,
        seekdb_host=os.getenv("SEEKDB_HOST", "127.0.0.1"),
        seekdb_port=int(os.getenv("SEEKDB_PORT", "2881")),
        seekdb_database=os.getenv("SEEKDB_DATABASE", "labrelay"),
        seekdb_user=os.getenv("SEEKDB_USER", "root"),
        seekdb_password=os.getenv("SEEKDB_ROOT_PASSWORD", ""),
        seekdb_tenant=os.getenv("SEEKDB_TENANT", "sys"),
    )
