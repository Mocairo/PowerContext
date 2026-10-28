from types import SimpleNamespace

import pytest
from powercontext.http import (
    ArtifactReference,
    HandoffContent,
    HandoffSelection,
    PreparedHandoff,
    SourceReference,
)

from labrelay.application.handoff import (
    HandoffAcknowledgementRequest,
    HandoffDraft,
    HandoffNotReadyError,
    HandoffService,
    HandoffStateItem,
    TaskOutcomeRequest,
)
from labrelay.domain.models import Citation, Project
from labrelay.persistence.memory import InMemoryRepository


def make_project() -> Project:
    return Project(
        id="project-1",
        title="Aurora",
        research_area="DL",
        description="baseline",
        project_scope_id="project-scope-1",
        learner_scope_id="learner-scope-1",
    )


def make_draft() -> HandoffDraft:
    citation = Citation(
        asset_id="asset-1",
        kind="code",
        title="train.py",
        uri="repo/train.py",
        version="sha",
        locator={"line_start": 1},
    )
    return HandoffDraft(
        objective="复现 baseline",
        state=[HandoffStateItem(text="已完成 baseline", citations=[citation])],
        next_action="推进扩展实验",
        omissions=["旧数据缓存可能影响数据划分"],
    )


def make_content() -> HandoffContent:
    return HandoffContent.model_validate(
        {
            "schema": "powercontext.handoff.v1",
            "objective": "复现 baseline",
            "state": [
                {
                    "text": "已完成 baseline",
                    "citations": [
                        {
                            "kind": "source",
                            "source_ref": {"name": "handoff-source", "source_id": "source-1"},
                        }
                    ],
                }
            ],
            "disposition": "continuable",
            "next_action": None,
            "omissions": [],
        }
    )


class RecordingContext:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.content = make_content()
        self.prepared = PreparedHandoff.model_validate(
            {
                "schema": "powercontext.prepared-handoff.v1",
                "scope_id": "project-scope-1",
                "base": None,
                "content": self.content,
            }
        )
        self.reference = ArtifactReference(family="handoff", artifact_id="h1", revision=1)

    async def remember(self, scope_id, kind, text, reason=None):
        self.calls.append(("remember", (scope_id, kind, text, reason)))

    async def create_source(self, scope_id: str, request):
        self.calls.append(("create_source", (scope_id, request)))
        return SimpleNamespace(source_id="source-1")

    async def create_contract(self, request):
        self.calls.append(("create_contract", request))
        return SimpleNamespace(kind="work-contract")

    async def prepare_handoff(self, request):
        self.calls.append(("prepare_handoff", request))
        return SimpleNamespace(handoff=self.prepared)

    async def commit_handoff(self, request):
        self.calls.append(("commit_handoff", request))
        return SimpleNamespace(reference=self.reference, content=self.content)

    async def continue_handoff(self, request):
        self.calls.append(("continue_handoff", request))
        return SimpleNamespace(content=self.content)

    async def acknowledge_handoff(self, request):
        self.calls.append(("acknowledge_handoff", request))
        return SimpleNamespace(
            receipt=SimpleNamespace(
                source=SourceReference(name="handoff-receipt", source_id="ack-1")
            )
        )

    async def record_outcome(self, request):
        self.calls.append(("record_outcome", request))
        return SimpleNamespace(source=object())


async def test_prepare_stores_draft_without_calling_powercontext() -> None:
    repository = InMemoryRepository()
    context = RecordingContext()
    service = HandoffService(repository, context)
    await repository.save_project(make_project())

    preview = await service.prepare("project-1", make_draft())

    assert preview.persisted is False
    assert preview.draft_id
    assert preview.objective == "复现 baseline"
    stored = await repository.get_handoff_draft(preview.draft_id)
    assert stored is not None
    assert stored["project_id"] == "project-1"
    assert stored["draft"]["objective"] == "复现 baseline"
    assert context.calls == []


async def test_confirm_creates_source_contract_handoff_and_saves_exact_ref() -> None:
    repository = InMemoryRepository()
    context = RecordingContext()
    service = HandoffService(repository, context)
    await repository.save_project(make_project())
    preview = await service.prepare("project-1", make_draft())

    result = await service.confirm("project-1", draft_id=preview.draft_id)

    assert [call[0] for call in context.calls] == [
        "remember",
        "create_source",
        "create_contract",
        "prepare_handoff",
        "commit_handoff",
    ]
    assert result.handoff_ref == context.reference.model_dump_json()
    assert result.scope_id == "project-scope-1"
    stored = await repository.get_project("project-1")
    assert stored is not None
    assert stored.handoff_ref == result.handoff_ref

    memory_scope, memory_kind, memory_text, memory_reason = context.calls[0][1]
    assert memory_scope == "project-scope-1"
    assert memory_kind == "handoff"
    assert "复现 baseline" in memory_text
    assert memory_reason == "handoff:project-1"
    source_scope, source_request = context.calls[1][1]
    assert source_scope == "project-scope-1"
    assert source_request.content["state"][0]["citations"][0]["asset_id"] == "asset-1"
    contract_request = context.calls[2][1]
    assert contract_request.contract.facts[0].basis == "declared"
    assert contract_request.contract.facts[0].evidence == []
    handoff_request = context.calls[3][1]
    assert handoff_request.handoff.state[0].text == "已完成 baseline"
    assert handoff_request.handoff.next_action.text == "推进扩展实验"
    commit_request = context.calls[4][1]
    assert commit_request.handoff == context.prepared


async def test_confirm_rejects_a_draft_from_another_project() -> None:
    repository = InMemoryRepository()
    context = RecordingContext()
    service = HandoffService(repository, context)
    await repository.save_project(make_project())
    other = make_project().model_copy(update={"id": "project-2", "title": "Other"})
    await repository.save_project(other)
    preview = await service.prepare("project-1", make_draft())

    with pytest.raises(HandoffNotReadyError, match="another project"):
        await service.confirm("project-2", draft_id=preview.draft_id)

    assert context.calls == []


async def test_confirm_without_draft_is_refused() -> None:
    repository = InMemoryRepository()
    context = RecordingContext()
    service = HandoffService(repository, context)
    await repository.save_project(make_project())

    with pytest.raises(HandoffNotReadyError):
        await service.confirm("project-1")

    assert context.calls == []


async def test_get_before_and_after_confirm() -> None:
    repository = InMemoryRepository()
    context = RecordingContext()
    service = HandoffService(repository, context)
    await repository.save_project(make_project())

    assert await service.get("project-1") is None

    await service.prepare("project-1", make_draft())
    committed = await service.confirm("project-1")
    context.calls.clear()
    view = await service.get("project-1")

    assert view is not None
    assert view.handoff_ref == committed.handoff_ref
    assert view.content is not None
    request = context.calls[0][1]
    assert request.selection == HandoffSelection.EXACT
    assert request.revision == context.reference


async def test_acknowledge_requires_committed_handoff_and_uses_exact_revision() -> None:
    repository = InMemoryRepository()
    context = RecordingContext()
    service = HandoffService(repository, context)
    await repository.save_project(make_project())
    request = HandoffAcknowledgementRequest(receiver="new-student")

    with pytest.raises(HandoffNotReadyError):
        await service.acknowledge("project-1", request)

    await service.prepare("project-1", make_draft())
    await service.confirm("project-1")
    context.calls.clear()
    result = await service.acknowledge("project-1", request)

    ack_request = context.calls[0][1]
    assert ack_request.selection.value == "exact"
    assert ack_request.revision == context.reference
    assert ack_request.receiver == "new-student"
    assert ack_request.status.value == "accepted"
    assert result.receipt_ref
    stored = await repository.get_project("project-1")
    assert stored is not None
    assert stored.handoff_receipt_ref == result.receipt_ref


async def test_record_outcome_links_the_acknowledged_receipt() -> None:
    repository = InMemoryRepository()
    context = RecordingContext()
    service = HandoffService(repository, context)
    await repository.save_project(make_project())
    await service.prepare("project-1", make_draft())
    await service.confirm("project-1")
    await service.acknowledge("project-1", HandoffAcknowledgementRequest(receiver="new-student"))
    context.calls.clear()
    request = TaskOutcomeRequest(
        objective="复现 baseline",
        status="succeeded",
        summary="指标一致",
        observations=["baseline 指标已核对"],
    )

    await service.record_outcome("project-1", request)

    outcome_request = context.calls[1][1]
    assert outcome_request.outcome.handoff_receipt_ref is not None
    assert outcome_request.outcome.handoff_receipt_ref.source_id == "ack-1"


async def test_record_outcome_creates_source_and_delegates() -> None:
    repository = InMemoryRepository()
    context = RecordingContext()
    service = HandoffService(repository, context)
    await repository.save_project(make_project())
    request = TaskOutcomeRequest(
        objective="复现 baseline",
        status="succeeded",
        summary="指标一致",
        observations=["baseline 指标已核对"],
        remaining_work=["扩展实验"],
    )

    await service.record_outcome("project-1", request)

    assert [call[0] for call in context.calls] == ["create_source", "record_outcome"]
    source_request = context.calls[0][1][1]
    assert source_request.content["summary"] == "指标一致"
    outcome_request = context.calls[1][1]
    assert outcome_request.outcome.status.value == "succeeded"
    assert outcome_request.outcome.observations[0].text == "baseline 指标已核对"
    assert outcome_request.outcome.remaining_work[0].root == "扩展实验"
