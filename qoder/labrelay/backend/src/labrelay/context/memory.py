"""Local ``ContextGateway`` used by tests and offline runs."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from powercontext.http import PreparedHandoff

from labrelay.domain.models import Project


class _ModelBox:
    """The tiny Pydantic surface the Handoff service reads from a adapter response."""

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def model_dump(self, **_: object) -> dict[str, object]:
        return dict(self._payload)

    def model_dump_json(self, **_: object) -> str:
        return json.dumps(self._payload, ensure_ascii=False)


class InMemoryContextGateway:
    """Deterministic memory scopes and notes without a PowerContext server."""

    def __init__(self) -> None:
        self._scopes: dict[str, tuple[str, str]] = {}
        self._memories: list[tuple[str, str, str, str | None]] = []
        self._sources: dict[str, object] = {}
        self._handoffs: dict[str, dict[str, object]] = {}

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

    async def create_source(self, scope_id: str, request: object) -> SimpleNamespace:
        source_id = f"source-{uuid4().hex}"
        self._sources[f"{scope_id}:{source_id}"] = getattr(request, "content", None)
        return SimpleNamespace(source_id=source_id)

    async def create_contract(self, request: object) -> None:
        del request
        return None

    async def prepare_handoff(self, request: Any) -> SimpleNamespace:
        handoff = request.handoff
        source_ref = {
            "kind": "source",
            "source_ref": {"name": "content", "source_id": request.source_id},
        }
        content = {
            "schema": "powercontext.handoff.v1",
            "objective": handoff.objective,
            "state": [{"text": item.text, "citations": [source_ref]} for item in handoff.state],
            "disposition": handoff.disposition,
            "next_action": {"text": handoff.next_action.text, "citations": [source_ref]},
            "omissions": [{"text": item.root, "citation": None} for item in handoff.omissions],
        }
        prepared = PreparedHandoff.model_validate(
            {
                "schema": "powercontext.prepared-handoff.v1",
                "scope_id": request.scope_id,
                "base": None,
                "content": content,
            }
        )
        return SimpleNamespace(handoff=prepared)

    async def commit_handoff(self, request: Any) -> SimpleNamespace:
        artifact_id = f"handoff-{uuid4().hex}"
        content = request.handoff.content.model_dump(mode="json", by_alias=True)
        self._handoffs[artifact_id] = content
        reference = _ModelBox({"family": "handoff", "artifact_id": artifact_id, "revision": 1})
        return SimpleNamespace(reference=reference, content=_ModelBox(content))

    async def continue_handoff(self, request: Any) -> SimpleNamespace:
        artifact_id = getattr(request.revision, "artifact_id", "")
        content = self._handoffs.get(artifact_id)
        return SimpleNamespace(content=None if content is None else _ModelBox(content))

    async def acknowledge_handoff(self, request: object) -> SimpleNamespace:
        source_id = f"source-{uuid4().hex}"
        receipt = {"name": "content", "source_id": source_id}
        return SimpleNamespace(receipt=SimpleNamespace(source=_ModelBox(receipt)))

    async def record_outcome(self, request: object) -> dict[str, str]:
        source_id = f"source-{uuid4().hex}"
        return {"status": "recorded", "outcome_source_id": source_id}
