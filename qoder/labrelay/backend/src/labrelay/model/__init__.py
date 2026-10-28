"""Model gateways behind the ``ModelGateway`` port (Task 12 imports from here)."""

from labrelay.model.fake import DeterministicModelGateway
from labrelay.model.openai_compat import OpenAICompatModelGateway

__all__ = ["DeterministicModelGateway", "OpenAICompatModelGateway"]
