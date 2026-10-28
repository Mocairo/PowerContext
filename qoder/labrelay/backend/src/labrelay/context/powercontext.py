"""Adapter from LabRelay's context port to the PowerContext 1.4 client."""

from __future__ import annotations

from typing import Any

from powercontext.client import (
    PowerContextClient,
    ServerResponseError,
    TransportError,
    UnavailableResponseError,
)
from powercontext.http import (
    AcknowledgeHandoffRequest,
    CommitHandoffRequest,
    ContinueHandoffRequest,
    CreateScopeRequest,
    CreateSourceRequest,
    CreateWorkContractRequest,
    HandoffCurrentWorkRequest,
    PrepareContextRequest,
    RecordTaskOutcomeRequest,
    RememberMemoryRequest,
)

from labrelay.config import LabRelayConfig
from labrelay.domain.models import Project

PROJECT_SCOPE_SUMMARY = "Project handoff, decisions and constraints"
LEARNER_SCOPE_SUMMARY = "Learner background, progress and blockers"
DEFAULT_CONTEXT_BYTES = 8000


class PowerContextUnavailableError(RuntimeError):
    """The PowerContext dependency could not answer; callers should surface 503."""


class PowerContextService:
    """Thin, typed wrapper around :class:`PowerContextClient`.

    The adapter keeps request construction and response shaping in one place; the
    application layer never sees the generated PowerContext models.
    """

    def __init__(self, client: PowerContextClient) -> None:
        self.client = client

    @classmethod
    def from_config(cls, config: LabRelayConfig) -> PowerContextService:
        return cls(
            PowerContextClient(
                base_url=config.powercontext_url,
                token=config.powercontext_token,
            )
        )

    async def _call(self, awaitable):
        """Translate transport and server-side availability failures into one error.

        A 4xx response is left untouched: it means the request itself is wrong, not that the
        service is down. Authentication/authorization failures are treated as unavailable so
        the API never claims a memory write succeeded with a bad token.
        """
        try:
            return await awaitable
        except (TransportError, UnavailableResponseError) as exc:
            raise PowerContextUnavailableError("PowerContext is unavailable") from exc
        except ServerResponseError as exc:
            status_code = getattr(exc, "status_code", 500)
            if status_code >= 500 or status_code in (401, 403):
                raise PowerContextUnavailableError("PowerContext is unavailable") from exc
            raise

    async def ensure_scopes(self, project: Project) -> tuple[str, str]:
        project_scope = await self._call(
            self.client.create_scope(
                CreateScopeRequest(
                    title=f"{project.title}:project:{project.id}",
                    summary=PROJECT_SCOPE_SUMMARY,
                    idempotency_key=f"labrelay:project:{project.id}",
                )
            )
        )
        learner_scope = await self._call(
            self.client.create_scope(
                CreateScopeRequest(
                    title=f"{project.title}:learner:{project.id}",
                    summary=LEARNER_SCOPE_SUMMARY,
                    idempotency_key=f"labrelay:learner:{project.id}",
                )
            )
        )
        return project_scope.scope_id, learner_scope.scope_id

    async def remember(
        self, scope_id: str, kind: str, text: str, reason: str | None = None
    ) -> None:
        await self._call(
            self.client.remember_memory(
                RememberMemoryRequest(
                    scope_id=scope_id,
                    kind=kind,
                    text=text,
                    reason=reason,
                )
            )
        )

    async def prepare_context(
        self, scope_id: str, query: str, max_bytes: int
    ) -> tuple[str | None, str | None]:
        prepared = await self._call(
            self.client.prepare_context(
                PrepareContextRequest(scope_id=scope_id, query=query, max_bytes=max_bytes)
            )
        )
        status = (
            prepared.status.value if hasattr(prepared.status, "value") else str(prepared.status)
        )
        return status, prepared.content

    async def project_context_summary(
        self, project_scope_id: str, learner_scope_id: str, query: str
    ) -> tuple[str | None, str]:
        _, project_content = await self.prepare_context(
            project_scope_id, query, DEFAULT_CONTEXT_BYTES
        )
        _, learner_content = await self.prepare_context(
            learner_scope_id, query, DEFAULT_CONTEXT_BYTES
        )
        parts = [content for content in (project_content, learner_content) if content]
        return ("\n".join(parts) or None, "powercontext")

    async def health_check(self) -> bool:
        try:
            await self.client.get_liveness()
        except Exception:
            return False
        return True

    async def aclose(self) -> None:
        """Release the SDK's HTTP resources when the app shuts down."""
        closer = getattr(self.client, "aclose", None) or getattr(self.client, "close", None)
        if closer is None:
            return
        result = closer()
        if hasattr(result, "__await__"):
            await result

    async def create_source(self, scope_id: str, request: CreateSourceRequest) -> Any:
        return await self._call(self.client.create_source(scope_id, request))

    async def create_contract(self, request: CreateWorkContractRequest) -> Any:
        return await self._call(self.client.create_work_contract(request))

    async def prepare_handoff(self, request: HandoffCurrentWorkRequest) -> Any:
        return await self._call(self.client.handoff_current_work(request))

    async def commit_handoff(self, request: CommitHandoffRequest) -> Any:
        return await self._call(self.client.commit_handoff(request))

    async def continue_handoff(self, request: ContinueHandoffRequest) -> Any:
        return await self._call(self.client.continue_handoff(request))

    async def acknowledge_handoff(self, request: AcknowledgeHandoffRequest) -> Any:
        return await self._call(self.client.acknowledge_handoff(request))

    async def record_outcome(self, request: RecordTaskOutcomeRequest) -> Any:
        return await self._call(self.client.record_task_outcome(request))
