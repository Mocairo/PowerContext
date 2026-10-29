"""Production composition root for the LabRelay backend.

The factory builds one immutable set of dependencies and hands it to the app. The API layer never
constructs a repository, engine or gateway itself, so swapping storage, context or model is an
environment choice rather than a route change.

Three switches compose the stack independently:

- ``LABRELAY_REPOSITORY``: ``memory`` or ``seekdb``
- ``LABRELAY_CONTEXT``: ``memory`` or ``powercontext``
- ``LABRELAY_MODEL_PROVIDER``: ``deterministic``, ``openai_compat`` or ``qoder``

Their defaults follow the run mode: fake dependencies stay fully in-memory, the real mode keeps
seekdb, PowerContext and the OpenAI-compatible gateway. The switches exist because the useful
local combination is not all-fake or all-real — a reviewer may want real PowerContext context and
a real Qwen model on top of an in-memory store.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from labrelay.application.handoff import HandoffService
from labrelay.application.services import LabRelayService
from labrelay.config import LabRelayConfig
from labrelay.context.memory import InMemoryContextGateway
from labrelay.context.powercontext import PowerContextService
from labrelay.model.fake import DeterministicModelGateway
from labrelay.model.openai_compat import OpenAICompatModelGateway
from labrelay.persistence.memory import InMemoryRepository
from labrelay.persistence.seekdb import SeekdbRepository
from labrelay.retrieval.in_memory import InMemorySearchEngine
from labrelay.retrieval.seekdb import SeekdbSearchEngine

REPOSITORY_KINDS = {"memory", "seekdb"}
CONTEXT_KINDS = {"memory", "powercontext"}
MODEL_KINDS = {"deterministic", "openai_compat", "qoder"}


@dataclass(slots=True)
class Runtime:
    """Everything the app owns for one process lifetime."""

    service: LabRelayService
    handoff: HandoffService
    repository: object
    engine: object
    context: object


def _choice(name: str, default: str) -> str:
    value = (os.getenv(name) or default).strip().lower()
    return value or default


def _build_model(kind: str, config: LabRelayConfig):
    if kind == "deterministic":
        return DeterministicModelGateway(config.embedding.dimensions or 2)
    if kind == "openai_compat":
        return OpenAICompatModelGateway.from_config(config)
    if kind == "qoder":
        from labrelay.model.qoder_agent import QoderAgentModelGateway

        return QoderAgentModelGateway.from_config(config)
    raise ValueError(f"LABRELAY_MODEL_PROVIDER must be one of {sorted(MODEL_KINDS)}, got {kind!r}")


def _build_repository(kind: str, config: LabRelayConfig):
    if kind == "memory":
        return InMemoryRepository()
    if kind == "seekdb":
        return SeekdbRepository.from_config(config)
    raise ValueError(f"LABRELAY_REPOSITORY must be one of {sorted(REPOSITORY_KINDS)}, got {kind!r}")


def _build_context(kind: str, config: LabRelayConfig):
    if kind == "memory":
        return InMemoryContextGateway()
    if kind == "powercontext":
        return PowerContextService.from_config(config)
    raise ValueError(f"LABRELAY_CONTEXT must be one of {sorted(CONTEXT_KINDS)}, got {kind!r}")


def build_runtime(config: LabRelayConfig, *, fake_dependencies: bool = False) -> Runtime:
    repository_kind = _choice("LABRELAY_REPOSITORY", "memory" if fake_dependencies else "seekdb")
    context_kind = _choice("LABRELAY_CONTEXT", "memory" if fake_dependencies else "powercontext")
    model_kind = _choice(
        "LABRELAY_MODEL_PROVIDER", "deterministic" if fake_dependencies else "openai_compat"
    )

    model = _build_model(model_kind, config)
    repository = _build_repository(repository_kind, config)
    engine = (
        InMemorySearchEngine(model)
        if repository_kind == "memory"
        else SeekdbSearchEngine.from_config(config, model=model)
    )
    context = _build_context(context_kind, config)

    service = LabRelayService(
        repository=repository,
        engine=engine,
        context=context,
        model=model,
        planner_config=config.planner,
    )
    return Runtime(
        service=service,
        handoff=HandoffService(repository, context),
        repository=repository,
        engine=engine,
        context=context,
    )


def build_service(config: LabRelayConfig, *, fake_dependencies: bool = False) -> LabRelayService:
    """Compatibility helper for callers that only need the use-case service."""
    return build_runtime(config, fake_dependencies=fake_dependencies).service


def describe(service: LabRelayService) -> dict[str, str]:
    """Name the dependency set a service is actually wired to, for the workbench status strip."""
    model = service.model
    class_name = type(model).__name__
    if class_name == "QoderAgentModelGateway":
        provider = "qoder"
    elif isinstance(model, OpenAICompatModelGateway):
        provider = "openai_compat"
    else:
        provider = "deterministic"
    return {
        "backend": "seekdb" if isinstance(service.repository, SeekdbRepository) else "memory",
        "context": "powercontext" if isinstance(service.context, PowerContextService) else "memory",
        "model": getattr(model, "model", None)
        or getattr(model, "generation_model", None)
        or class_name,
        "model_provider": provider,
    }


__all__ = ["Runtime", "build_runtime", "build_service", "describe"]
