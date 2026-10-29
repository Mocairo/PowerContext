"""Production composition root for the LabRelay backend.

The factory builds one immutable set of dependencies and hands it to the app. The API layer never
constructs a repository, engine or gateway itself, so swapping the in-memory demo for seekdb,
PowerContext and the Qoder/OpenAI gateway is a configuration choice rather than a route change.
"""

from __future__ import annotations

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


@dataclass(slots=True)
class Runtime:
    """Everything the app owns for one process lifetime."""

    service: LabRelayService
    handoff: HandoffService
    repository: object
    engine: object
    context: object


def build_runtime(config: LabRelayConfig, *, fake_dependencies: bool = False) -> Runtime:
    if fake_dependencies:
        repository = InMemoryRepository()
        model = DeterministicModelGateway(config.embedding.dimensions or 2)
        engine = InMemorySearchEngine(model)
        context = InMemoryContextGateway()
    else:
        repository = SeekdbRepository.from_config(config)
        model = OpenAICompatModelGateway.from_config(config)
        engine = SeekdbSearchEngine.from_config(config, model=model)
        context = PowerContextService.from_config(config)

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
    """Name the dependency set a service is actually wired to, for the demo panel's status call.

    The panel has to say which stack it is talking to, and this module is the only one allowed to
    know the concrete classes — the API layer must not import them to find out.
    """
    return {
        "backend": "seekdb" if isinstance(service.repository, SeekdbRepository) else "memory",
        "context": "powercontext" if isinstance(service.context, PowerContextService) else "memory",
        "model": getattr(service.model, "model", None) or type(service.model).__name__,
        "model_provider": (
            "openai_compat"
            if isinstance(service.model, OpenAICompatModelGateway)
            else "deterministic"
        ),
    }


__all__ = ["Runtime", "build_runtime", "build_service", "describe"]
