"""Local ``ContextGateway`` used by tests and offline runs."""

from __future__ import annotations

from labrelay.domain.models import Project


class InMemoryContextGateway:
    """Deterministic memory scopes and notes without a PowerContext server."""

    def __init__(self) -> None:
        self._scopes: dict[str, tuple[str, str]] = {}
        self._memories: list[tuple[str, str, str, str | None]] = []

    async def ensure_scopes(self, project: Project) -> tuple[str, str]:
        return self._scopes.setdefault(
            project.id,
            (f"project:{project.id}", f"learner:{project.id}"),
        )

    async def remember(
        self, scope_id: str, kind: str, text: str, reason: str | None = None
    ) -> None:
        self._memories.append((scope_id, kind, text, reason))

    async def prepare_context(
        self, scope_id: str, query: str, max_bytes: int
    ) -> tuple[str | None, str | None]:
        del query, max_bytes
        text = "\n".join(note for scope, _, note, _ in self._memories if scope == scope_id)
        return ("ready", text) if text else (None, None)

    async def project_context_summary(
        self, project_scope_id: str, learner_scope_id: str, query: str
    ) -> tuple[str | None, str]:
        del query
        wanted = {project_scope_id, learner_scope_id}
        text = "\n".join(note for scope, _, note, _ in self._memories if scope in wanted)
        return (text or None, "in_memory")

    async def health_check(self) -> bool:
        return True

    @property
    def memories(self) -> list[tuple[str, str, str, str | None]]:
        return list(self._memories)
