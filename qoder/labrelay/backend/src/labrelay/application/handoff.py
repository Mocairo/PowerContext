"""Handoff use cases: prepare, commit the exact revision, acknowledge and record outcomes."""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

from powercontext.http import (
    AcknowledgeHandoffRequest,
    ArtifactReference,
    CommitHandoffRequest,
    ContinueHandoffRequest,
    CreateSourceRequest,
    CreateWorkContractRequest,
    CurrentWorkHandoff,
    HandoffAcknowledgementSelection,
    HandoffCurrentWorkRequest,
    HandoffReceiptStatus,
    HandoffSelection,
    ReceiverChecks,
    RecordTaskOutcomeRequest,
    TaskOutcome,
)
from pydantic import BaseModel, Field

from labrelay.context.powercontext import PowerContextService
from labrelay.domain.models import Citation, Project
from labrelay.domain.ports import Repository


class HandoffStateItem(BaseModel):
    text: str = Field(min_length=1)
    citations: list[Citation] = []


class HandoffDraft(BaseModel):
    objective: str = Field(min_length=1)
    state: list[HandoffStateItem] = Field(min_length=1)
    next_action: str = Field(min_length=1)
    omissions: list[str] = []
    disposition: Literal["continuable", "blocked", "complete"] = "continuable"


class HandoffPreview(BaseModel):
    project_id: str
    draft: HandoffDraft
    persisted: bool = False


class HandoffView(BaseModel):
    project_id: str
    handoff_ref: str
    content: dict[str, object] | None = None


class HandoffAcknowledgementRequest(BaseModel):
    receiver: str = Field(min_length=1)
    status: Literal["accepted", "needs_clarification", "declined"] = "accepted"
    message: str | None = None
    receiver_checks: dict[str, str] | None = None


class TaskOutcomeRequest(BaseModel):
    objective: str = Field(min_length=1)
    status: Literal["succeeded", "partial", "blocked", "failed", "cancelled", "unknown"]
    summary: str = Field(min_length=1)
    observations: list[str] = Field(min_length=1)
    remaining_work: list[str] = []


class HandoffNotReadyError(ValueError):
    """The requested operation needs a prepared draft or a committed Handoff first."""


class HandoffService:
    """Application-level Handoff workflow backed by PowerContext and the project store."""

    def __init__(self, repository: Repository, context: PowerContextService) -> None:
        self.repository = repository
        self.context = context
        self._drafts: dict[str, HandoffDraft] = {}

    async def prepare(self, project_id: str, draft: HandoffDraft) -> HandoffPreview:
        project = await self._project(project_id)
        self._drafts[project.id] = draft.model_copy(deep=True)
        return HandoffPreview(project_id=project.id, draft=draft)

    async def confirm(self, project_id: str, draft: HandoffDraft | None = None) -> HandoffView:
        project = await self._project(project_id)
        chosen = draft or self._drafts.get(project.id)
        if chosen is None:
            raise HandoffNotReadyError("No prepared handoff draft exists for this project.")
        if not project.project_scope_id:
            raise HandoffNotReadyError("The project has no PowerContext project scope.")

        source_content = self._source_content(chosen)
        source = await self.context.create_source(
            project.project_scope_id, CreateSourceRequest(content=source_content)
        )
        contract = WorkContractPayload.from_draft(chosen)
        await self.context.create_contract(
            CreateWorkContractRequest(
                scope_id=project.project_scope_id,
                source_id=source.source_id,
                contract=contract,
            )
        )
        current_work = CurrentWorkHandoffPayload.from_draft(chosen)
        prepared = await self.context.prepare_handoff(
            HandoffCurrentWorkRequest(
                scope_id=project.project_scope_id,
                source_id=source.source_id,
                handoff=current_work,
            )
        )
        committed = await self.context.commit_handoff(
            CommitHandoffRequest(
                scope_id=project.project_scope_id,
                handoff=prepared.handoff,
            )
        )

        handoff_ref = committed.reference.model_dump_json()
        updated = project.model_copy(update={"handoff_ref": handoff_ref})
        await self.repository.save_project(updated)
        self._drafts.pop(project.id, None)
        return HandoffView(
            project_id=project.id,
            handoff_ref=handoff_ref,
            content=committed.content.model_dump(mode="json", by_alias=True),
        )

    async def get(self, project_id: str) -> HandoffView | None:
        project = await self._project(project_id)
        if not project.handoff_ref:
            return None
        reference = ArtifactReference.model_validate_json(project.handoff_ref)
        resolution = await self.context.continue_handoff(
            ContinueHandoffRequest(
                scope_id=project.project_scope_id,
                selection=HandoffSelection.EXACT,
                revision=reference,
            )
        )
        content = (
            resolution.content.model_dump(mode="json", by_alias=True)
            if resolution.content is not None
            else None
        )
        return HandoffView(
            project_id=project.id,
            handoff_ref=project.handoff_ref,
            content=content,
        )

    async def acknowledge(self, project_id: str, request: HandoffAcknowledgementRequest):
        project = await self._project(project_id)
        if not project.handoff_ref:
            raise HandoffNotReadyError("No committed Handoff exists for this project.")
        reference = ArtifactReference.model_validate_json(project.handoff_ref)
        checks = (
            ReceiverChecks.model_validate(request.receiver_checks)
            if request.receiver_checks
            else None
        )
        return await self.context.acknowledge_handoff(
            AcknowledgeHandoffRequest(
                scope_id=project.project_scope_id,
                source_id=f"handoff-ack-{uuid4().hex}",
                receiver=request.receiver,
                status=HandoffReceiptStatus(request.status),
                selection=HandoffAcknowledgementSelection.EXACT,
                revision=reference,
                receiver_checks=checks,
                message=request.message,
            )
        )

    async def record_outcome(self, project_id: str, request: TaskOutcomeRequest):
        project = await self._project(project_id)
        if not project.project_scope_id:
            raise HandoffNotReadyError("The project has no PowerContext project scope.")
        source = await self.context.create_source(
            project.project_scope_id,
            CreateSourceRequest(content=request.model_dump(mode="json")),
        )
        outcome = TaskOutcome.model_validate(
            {
                "schema": "powercontext.task-outcome.v1",
                "trust": "untrusted_observation",
                "objective": request.objective,
                "status": request.status,
                "summary": request.summary,
                "observations": [
                    {"text": text, "basis": "declared", "evidence": []}
                    for text in request.observations
                ],
                "checks": [],
                "produced_artifacts": [],
                "remaining_work": request.remaining_work,
            }
        )
        return await self.context.record_outcome(
            RecordTaskOutcomeRequest(
                scope_id=project.project_scope_id,
                source_id=source.source_id,
                outcome=outcome,
            )
        )

    async def _project(self, project_id: str) -> Project:
        project = await self.repository.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        return project

    @staticmethod
    def _source_content(draft: HandoffDraft) -> dict[str, object]:
        return {
            "objective": draft.objective,
            "state": [
                {
                    "text": item.text,
                    "citations": [citation.model_dump(mode="json") for citation in item.citations],
                }
                for item in draft.state
            ],
            "next_action": draft.next_action,
            "omissions": draft.omissions,
            "disposition": draft.disposition,
            "citation_semantics": "labrelay citations, not PowerContext artifact refs",
        }


class WorkContractPayload:
    """Build the PowerContext WorkContract from LabRelay's draft.

    LabRelay citations are not native PowerContext ArtifactRefs, so the claims keep an empty
    evidence list and the citations travel in the Source created immediately before this
    contract. The claim basis still records whether the mentor supplied evidence.
    """

    @staticmethod
    def from_draft(draft: HandoffDraft):
        from powercontext.http import WorkContract

        return WorkContract.model_validate(
            {
                "schema": "powercontext.work-contract.v1",
                "trust": "untrusted_input",
                "objective": draft.objective,
                "facts": [
                    {
                        "text": item.text,
                        "basis": "verified" if item.citations else "declared",
                        "evidence": [],
                    }
                    for item in draft.state
                ],
                "in_scope": [draft.next_action],
                "exclusions": draft.omissions,
                "completion_criteria": ["接手人能够说明当前状态、证据和下一步"],
                "authorization_notes": ["未授权自动操作实验室服务器或资源"],
                "open_questions": draft.omissions,
            }
        )


class CurrentWorkHandoffPayload:
    @staticmethod
    def from_draft(draft: HandoffDraft) -> CurrentWorkHandoff:
        return CurrentWorkHandoff.model_validate(
            {
                "schema": "powercontext.current-work-handoff.v1",
                "trust": "untrusted_input",
                "objective": draft.objective,
                "state": [
                    {
                        "text": item.text,
                        "basis": "verified" if item.citations else "declared",
                        "evidence": [],
                    }
                    for item in draft.state
                ],
                "disposition": draft.disposition,
                "next_action": {
                    "text": draft.next_action,
                    "basis": "declared",
                    "evidence": [],
                },
                "omissions": draft.omissions,
            }
        )
