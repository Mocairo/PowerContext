# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Persistent, scope-local source withdrawal and dependent Memory eligibility."""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.persistence.source_lifecycle import SourceLifecycleStore
from powercontext.builtin.records import BaseValueNotFoundError
from powercontext.builtin.runtime.models import GetMemoryEntryRequest
from powercontext.builtin.sources import validate_scope_id
from powercontext.errors import PowerContextError
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import BuiltinRuntime


class SourceLifecycleError(ValueError):
    """A lifecycle registration, event, or reference cannot be accepted."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"source lifecycle: {reason}")


class SourceLifecycleEvent(BaseModel):
    """An exact, strictly typed transition of one registered Source revision."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    node_id: str
    revision: int = Field(gt=0)
    sequence: int = Field(gt=0)
    available: bool
    reason: str

    @field_validator("node_id", "reason")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("lifecycle text must not be blank")  # noqa: TRY003
        return value


class _Node(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["source", "memory", "topic"]
    reference: SourceRef | MemoryCitation | ArtifactRef
    parents: tuple[str, ...] = ()
    policy: Literal["all", "any"] = "all"
    revision: int | None = None
    available: bool = True
    event: SourceLifecycleEvent | None = None


class _Graph(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    nodes: dict[str, _Node] = Field(default_factory=dict)


def _node_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceLifecycleError("node_id-must-be-a-nonblank-string")
    return value


def _native_identity(node: _Node) -> tuple[str, ...] | None:
    reference = node.reference
    if isinstance(reference, MemoryCitation):
        return ("memory", reference.memory_ref.artifact_id, reference.entry_id, reference.entry_version_id)
    if isinstance(reference, ArtifactRef):
        return ("topic", reference.artifact_id, str(reference.revision))
    return None


def _derive(graph: _Graph) -> None:
    done: set[str] = set()
    visiting: set[str] = set()

    def visit(node_id: str) -> bool:
        if node_id in visiting:
            raise SourceLifecycleError("dependency-cycle")
        node = graph.nodes[node_id]
        if node_id in done:
            return node.available
        visiting.add(node_id)
        if node.kind != "source":
            # Visit every parent even for any/all so cycle validation never
            # depends on short-circuit evaluation or current availability.
            support = tuple(visit(parent) for parent in node.parents)
            node = node.model_copy(update={"available": all(support) if node.policy == "all" else any(support)})
            graph.nodes[node_id] = node
        visiting.remove(node_id)
        done.add(node_id)
        return node.available

    for node_id in graph.nodes:
        visit(node_id)


class ScopedSourceLifecycle:
    """Apply dependency events through native Memory state transitions."""

    def __init__(self, runtime: BuiltinRuntime, store: SourceLifecycleStore | None, scope_id: str) -> None:
        self._runtime = runtime
        self._store = store
        self.scope_id = validate_scope_id(scope_id)

    def _storage(self) -> SourceLifecycleStore:
        if self._store is None:
            raise SourceLifecycleError("lifecycle-persistence-is-not-configured")
        return self._store

    async def _load(self) -> _Graph:
        payload = await self._storage().load(self.scope_id)
        return _Graph() if payload is None else _Graph.model_validate_json(payload)

    async def _save(self, graph: _Graph) -> None:
        unavailable = tuple(
            node.reference
            for node in graph.nodes.values()
            if node.kind == "topic" and not node.available and isinstance(node.reference, ArtifactRef)
        )
        await self._storage().save(self.scope_id, graph.model_dump_json().encode(), unavailable)

    async def register_source(self, node_id: str, source_ref: SourceRef, *, revision: int) -> None:
        if not isinstance(source_ref, SourceRef):
            raise SourceLifecycleError("source-registration-requires-a-SourceRef")
        if type(revision) is not int or revision <= 0:
            raise SourceLifecycleError("revision-must-be-a-positive-integer")
        await self._register(_node_id(node_id), _Node(kind="source", reference=source_ref, revision=revision))

    async def register_memory(
        self, node_id: str, citation: MemoryCitation, *, parents: tuple[str, ...], policy: Literal["all", "any"]
    ) -> None:
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("memory-registration-requires-a-MemoryCitation")
        await self._register_derived(node_id, "memory", citation, parents, policy)

    async def register_topic(
        self, node_id: str, artifact_ref: ArtifactRef, *, parents: tuple[str, ...], policy: Literal["all", "any"]
    ) -> None:
        if not isinstance(artifact_ref, ArtifactRef) or artifact_ref.family != "topic-memory":
            raise SourceLifecycleError("topic-registration-requires-a-Topic-Memory-ArtifactRef")
        await self._register_derived(node_id, "topic", artifact_ref, parents, policy)

    async def _register_derived(
        self,
        node_id: str,
        kind: Literal["memory", "topic"],
        reference: MemoryCitation | ArtifactRef,
        parents: tuple[str, ...],
        policy: Literal["all", "any"],
    ) -> None:
        _node_id(node_id)
        if not isinstance(parents, (tuple, list)) or not parents:
            raise SourceLifecycleError("derived-nodes-require-parents")
        for parent in parents:
            _node_id(parent)
        if len(set(parents)) != len(parents) or node_id in parents:
            raise SourceLifecycleError("duplicate-or-self-dependency")
        if policy not in ("all", "any"):
            raise SourceLifecycleError("unknown-dependency-policy")
        await self._register(
            node_id, _Node(kind=kind, reference=reference, parents=tuple(sorted(parents)), policy=policy)
        )

    async def _register(self, node_id: str, node: _Node) -> None:
        async with self._runtime._scope_operation(self.scope_id), self._runtime._locked(self.scope_id):
            graph = await self._load()
            previous = graph.nodes.get(node_id)
            if previous is not None:
                if previous.model_copy(update={"available": True, "event": None}) != node:
                    raise SourceLifecycleError("node_id-is-already-bound-differently")
            elif any(parent not in graph.nodes for parent in node.parents):
                raise SourceLifecycleError("parents-must-be-registered-first")
            identity = _native_identity(node)
            if previous is None and identity is not None:
                for registered in graph.nodes.values():
                    if _native_identity(registered) == identity and (
                        set(registered.parents) != set(node.parents) or registered.policy != node.policy
                    ):
                        # One native body has one eligibility state. Equivalent
                        # aliases are safe; contradictory dependency bindings
                        # cannot be represented without rewriting that body.
                        raise SourceLifecycleError("conflicting-native-eligibility-binding")
            await self._validate_reference(node)
            if previous is None:
                graph.nodes[node_id] = node
                _derive(graph)
                await self._save(graph)
            # A repeated registration also repairs an interrupted initial
            # deactivation. Original references are never replaced by new heads.
            await self._reconcile(graph, reason="source lifecycle registration")

    async def _validate_reference(self, node: _Node) -> None:
        reference = node.reference
        try:
            if isinstance(reference, SourceRef):
                async with self._runtime._context(self.scope_id) as context:
                    await context.sources.read(await self._storage().source(self.scope_id, reference))
            elif isinstance(reference, MemoryCitation):
                await self._runtime.memory.for_scope(self.scope_id).get(GetMemoryEntryRequest(citation=reference))
                async with self._runtime._context(self.scope_id) as context:
                    service = context.artifacts.memory
                    current = await service.head(reference.memory_ref.artifact_id)
                    if not any(
                        entry.entry_id == reference.entry_id and entry.entry_version_id == reference.entry_version_id
                        for entry in current.content.manifest.entries
                    ):
                        raise SourceLifecycleError("registered-entry-version-must-still-be-current")
            else:
                await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
                    reference.family, reference.artifact_id, reference.revision
                )
        except (PowerContextError, BaseValueNotFoundError, LookupError, ValueError) as error:
            raise SourceLifecycleError("registration-reference-is-not-readable-in-this-scope") from error

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("apply-requires-a-SourceLifecycleEvent")
        # Revalidate even model_construct/model_copy values at the write boundary.
        event = SourceLifecycleEvent.model_validate(event.model_dump())
        async with self._runtime._scope_operation(self.scope_id), self._runtime._locked(self.scope_id):
            graph = await self._load()
            node = graph.nodes.get(event.node_id)
            if node is None or node.kind != "source" or node.revision != event.revision:
                raise SourceLifecycleError("unknown-Source-or-mismatched-lifecycle-revision")
            if node.event is not None and (
                event.sequence < node.event.sequence or (event.sequence == node.event.sequence and event != node.event)
            ):
                raise SourceLifecycleError("obsolete-or-conflicting-lifecycle-event")
            if node.event != event:
                graph.nodes[event.node_id] = node.model_copy(update={"available": event.available, "event": event})
                _derive(graph)
                # Persist intent before native writes. A crash can leave partial
                # progress, but cannot forget an accepted sequence. Replaying
                # reconciles actual native state, not a cached completion flag.
                await self._save(graph)
            await self._reconcile(graph, reason=event.reason)
            return sorted(node_id for node_id, node in graph.nodes.items() if node.available)

    async def _reconcile(self, graph: _Graph, *, reason: str) -> None:
        targets: dict[tuple[str, str], bool] = {}
        for node in graph.nodes.values():
            if isinstance(node.reference, MemoryCitation):
                key = (node.reference.entry_id, node.reference.entry_version_id)
                targets[key] = targets.get(key, True) and node.available
        if not targets:
            return
        async with self._runtime._context(self.scope_id) as context:
            service = context.artifacts.memory
            current = await service.head(context.artifacts.memory_artifact_id)
            entries = {(entry.entry_id, entry.entry_version_id): entry for entry in await service.entries(current)}
            if targets.keys() - entries.keys():
                raise SourceLifecycleError("registered-Memory-entry-version-is-no-longer-current")
            for available in (False, True):
                state = "active" if available else "inactive"
                current_states = {entry.entry_id: entry.state for entry in current.content.manifest.entries}
                changes = tuple(
                    entries[key]
                    for key, target in targets.items()
                    if target == available and current_states[key[0]] != state
                )
                if changes:
                    transition = service.reactivate if available else service.forget
                    # Memory change reasons have a 512-character bound. The
                    # complete, unmodified event reason is in the durable graph.
                    native_reason = unicodedata.normalize("NFC", reason).strip()[:512]
                    current = await transition(current, entries=changes, reason=native_reason)

    async def resolve(
        self, node_id: str, *, purpose: Literal["generation", "audit"]
    ) -> SourceRef | MemoryCitation | ArtifactRef:
        _node_id(node_id)
        if purpose not in ("generation", "audit"):
            raise SourceLifecycleError("unknown-lifecycle-purpose")
        async with self._runtime._scope_operation(self.scope_id):
            node = (await self._load()).nodes.get(node_id)
            if node is None:
                raise SourceLifecycleError("unknown-lifecycle-node")
            if purpose == "generation" and not node.available:
                raise SourceLifecycleError("node-is-unavailable-for-generation")
            return node.reference


class SourceLifecycleApplication:
    """Select one durable lifecycle graph by Scope."""

    def __init__(self, runtime: BuiltinRuntime, store: SourceLifecycleStore | None) -> None:
        self._runtime = runtime
        self._store = store

    def for_scope(self, scope_id: str) -> ScopedSourceLifecycle:
        return ScopedSourceLifecycle(self._runtime, self._store, scope_id)


__all__ = ["ScopedSourceLifecycle", "SourceLifecycleApplication", "SourceLifecycleError", "SourceLifecycleEvent"]
