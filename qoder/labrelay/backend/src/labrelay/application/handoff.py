"""Handoff use cases: prepare, commit the exact revision, acknowledge and record outcomes."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import uuid4

from powercontext.builtin.work import (
    HANDOFF_RECEIPT_SOURCE_KIND,
)
from powercontext.builtin.work import (
    CurrentWorkHandoff as StrictCurrentWorkHandoff,
)
from powercontext.builtin.work import (
    TaskOutcome as StrictTaskOutcome,
)
from powercontext.builtin.work import (
    WorkContract as StrictWorkContract,
)
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
    SourceReference,
    TaskOutcome,
)
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from labrelay.context.powercontext import PowerContextService
from labrelay.domain.models import Citation, Project
from labrelay.domain.ports import Repository

NonEmptyText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8192)
]
ReceiverName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)
]


class HandoffStateItem(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    text: NonEmptyText
    citations: list[Citation] = Field(default_factory=list, max_length=64)


class HandoffDraft(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    draft_id: str | None = Field(default=None, max_length=64)
    objective: NonEmptyText
    state: list[HandoffStateItem] = Field(min_length=1, max_length=64)
    next_action: NonEmptyText
    omissions: list[NonEmptyText] = Field(default_factory=list, max_length=64)
    disposition: Literal["continuable", "blocked", "complete"] = "continuable"


class HandoffPreview(BaseModel):
    project_id: str
    draft_id: str
    objective: str
    state: list[HandoffStateItem]
    next_action: str
    omissions: list[str]
    disposition: Literal["continuable", "blocked", "complete"] = "continuable"
    persisted: bool = False


class HandoffConfirmRequest(BaseModel):
    draft_id: str | None = Field(default=None, max_length=64)
    draft: HandoffDraft | None = None


class HandoffView(BaseModel):
    project_id: str
    scope_id: str
    handoff_ref: str
    content: dict[str, object] | None = None


class ReceiverCheckValues(BaseModel):
    live_state: Literal["confirmed", "mismatch", "not_checked"] = "not_checked"
    capability: Literal["confirmed", "insufficient", "not_checked"] = "not_checked"
    authorization: Literal["confirmed", "insufficient", "not_checked"] = "not_checked"


class HandoffAcknowledgementRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    receiver: ReceiverName
    status: Literal["accepted", "needs_clarification", "declined"] = "accepted"
    message: NonEmptyText | None = None
    receiver_checks: ReceiverCheckValues | None = None

    @model_validator(mode="after")
    def mirror_powercontext_acknowledgement_rules(self) -> HandoffAcknowledgementRequest:
        # PowerContext rejects an accepted Handoff without all three receiver checks
        # confirmed, and a non-accepted one without a message; reject here so the
        # request never fails on the wire as a 500.
        if self.status == "accepted":
            checks = self.receiver_checks
            if checks is None or not (
                checks.live_state == "confirmed"
                and checks.capability == "confirmed"
                and checks.authorization == "confirmed"
            ):
                raise ValueError("accepted acknowledgement requires all receiver checks confirmed")
        elif self.message is None:
            raise ValueError("non-accepted acknowledgement requires a message")
        return self


class HandoffAcknowledgementView(BaseModel):
    receipt_ref: str
    status: str
    message: str | None = None


class TaskOutcomeRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    objective: NonEmptyText
    status: Literal["succeeded", "partial", "blocked", "failed", "cancelled", "unknown"]
    summary: NonEmptyText
    observations: list[NonEmptyText] = Field(min_length=1, max_length=64)
    remaining_work: list[NonEmptyText] = Field(default_factory=list, max_length=64)


class HandoffNotReadyError(ValueError):
    """The requested operation needs a prepared draft or a committed Handoff first."""


class HandoffService:
    """Application-level Handoff workflow backed by PowerContext and the project store."""

    def __init__(self, repository: Repository, context: PowerContextService) -> None:
        self.repository = repository
        self.context = context
        self._last_draft_ids: dict[str, str] = {}

    async def prepare(self, project_id: str, draft: HandoffDraft) -> HandoffPreview:
        project = await self._project(project_id)
        draft_id = f"handoff-draft-{uuid4().hex}"
        stored = draft.model_copy(update={"draft_id": draft_id}, deep=True)
        await self.repository.save_handoff_draft(
            draft_id,
            project.id,
            {"draft": stored.model_dump(mode="json")},
        )
        self._last_draft_ids[project.id] = draft_id
        return HandoffPreview(
            project_id=project.id,
            draft_id=draft_id,
            objective=stored.objective,
            state=stored.state,
            next_action=stored.next_action,
            omissions=stored.omissions,
            disposition=stored.disposition,
        )

    async def confirm(
        self,
        project_id: str,
        draft_id: str | None = None,
        draft: HandoffDraft | None = None,
    ) -> HandoffView:
        project = await self._project(project_id)
        chosen = draft
        if chosen is None:
            resolved_id = draft_id or self._last_draft_ids.get(project.id)
            if resolved_id is None:
                raise HandoffNotReadyError("No prepared handoff draft exists for this project.")
            stored = await self.repository.get_handoff_draft(resolved_id)
            if stored is None:
                raise HandoffNotReadyError("The Handoff draft no longer exists.")
            if stored.get("project_id") != project.id:
                raise HandoffNotReadyError("The Handoff draft belongs to another project.")
            payload = stored.get("draft")
            if not isinstance(payload, dict):
                raise HandoffNotReadyError("The stored Handoff draft is malformed.")
            chosen = HandoffDraft.model_validate(payload)
        if not project.project_scope_id:
            raise HandoffNotReadyError("The project has no PowerContext project scope.")

        # Validate both strict payloads before the first write, so a bad draft cannot leave
        # an orphan Source in PowerContext.
        contract = WorkContractPayload.from_draft(chosen)
        current_work = CurrentWorkHandoffPayload.from_draft(chosen)
        await self.context.remember(
            project.project_scope_id,
            "handoff",
            f"确认交接：{chosen.objective}；下一步：{chosen.next_action}",
            reason=f"handoff:{project.id}",
        )
        source_content = self._source_content(chosen)
        source = await self.context.create_source(
            project.project_scope_id, CreateSourceRequest(content=source_content)
        )
        await self.context.create_contract(
            CreateWorkContractRequest(
                scope_id=project.project_scope_id,
                source_id=source.source_id,
                contract=contract,
            )
        )
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
        self._last_draft_ids.pop(project.id, None)
        return HandoffView(
            project_id=project.id,
            scope_id=project.project_scope_id,
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
            scope_id=project.project_scope_id,
            handoff_ref=project.handoff_ref,
            content=content,
        )

    async def acknowledge(self, project_id: str, request: HandoffAcknowledgementRequest):
        project = await self._project(project_id)
        if not project.handoff_ref:
            raise HandoffNotReadyError("No committed Handoff exists for this project.")
        reference = ArtifactReference.model_validate_json(project.handoff_ref)
        checks = (
            ReceiverChecks.model_validate(request.receiver_checks.model_dump())
            if request.receiver_checks
            else None
        )
        result = await self.context.acknowledge_handoff(
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
        receipt_ref = result.receipt.source.model_dump_json()
        updated = project.model_copy(update={"handoff_receipt_ref": receipt_ref})
        await self.repository.save_project(updated)
        return HandoffAcknowledgementView(
            receipt_ref=receipt_ref,
            status=request.status,
            message=request.message,
        )

    async def record_outcome(self, project_id: str, request: TaskOutcomeRequest):
        project = await self._project(project_id)
        if not project.project_scope_id:
            raise HandoffNotReadyError("The project has no PowerContext project scope.")
        source = await self.context.create_source(
            project.project_scope_id,
            CreateSourceRequest(content=request.model_dump(mode="json")),
        )
        receipt_ref = (
            SourceReference.model_validate_json(project.handoff_receipt_ref)
            if project.handoff_receipt_ref
            else None
        )
        outcome_payload = {
            "schema": "powercontext.task-outcome.v1",
            "trust": "untrusted_observation",
            "objective": request.objective,
            "status": request.status,
            "summary": request.summary,
            "handoff_receipt_ref": (receipt_ref.model_dump(mode="json") if receipt_ref else None),
            "observations": [
                {"text": text, "basis": "declared", "evidence": []} for text in request.observations
            ],
            "checks": [],
            "produced_artifacts": [],
            "remaining_work": request.remaining_work,
        }
        strict_payload = {**outcome_payload}
        if receipt_ref:
            strict_payload["handoff_receipt_ref"] = {
                "source_type": HANDOFF_RECEIPT_SOURCE_KIND,
                "source_id": receipt_ref.source_id,
            }
        StrictTaskOutcome.model_validate(_as_strict(strict_payload))
        outcome = TaskOutcome.model_validate(outcome_payload)
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


def _as_strict(value):
    """PowerContext's server-side models use tuples; the HTTP models accept JSON lists."""
    if isinstance(value, list):
        return tuple(_as_strict(item) for item in value)
    if isinstance(value, dict):
        return {key: _as_strict(item) for key, item in value.items()}
    return value


class WorkContractPayload:
    """Build the PowerContext WorkContract from LabRelay's draft.

    LabRelay citations are not native PowerContext ArtifactRefs, so claims are ``declared``
    with an empty evidence list; the citations travel in the Source created immediately before
    this contract. Both the HTTP request model and the server's stricter builtin model validate
    the payload before any write happens.
    """

    @staticmethod
    def from_draft(draft: HandoffDraft):
        from powercontext.http import WorkContract

        payload = {
            "schema": "powercontext.work-contract.v1",
            "trust": "untrusted_input",
            "objective": draft.objective,
            "facts": [
                {"text": item.text, "basis": "declared", "evidence": []} for item in draft.state
            ],
            "in_scope": [draft.next_action],
            "exclusions": draft.omissions,
            "completion_criteria": ["接手人能够说明当前状态、证据和下一步"],
            "authorization_notes": ["未授权自动操作实验室服务器或资源"],
            "open_questions": draft.omissions,
        }
        StrictWorkContract.model_validate(_as_strict(payload))
        return WorkContract.model_validate(payload)


class CurrentWorkHandoffPayload:
    @staticmethod
    def from_draft(draft: HandoffDraft) -> CurrentWorkHandoff:
        payload = {
            "schema": "powercontext.current-work-handoff.v1",
            "trust": "untrusted_input",
            "objective": draft.objective,
            "state": [
                {"text": item.text, "basis": "declared", "evidence": []} for item in draft.state
            ],
            "disposition": draft.disposition,
            "next_action": {
                "text": draft.next_action,
                "basis": "declared",
                "evidence": [],
            },
            "omissions": draft.omissions,
        }
        StrictCurrentWorkHandoff.model_validate(_as_strict(payload))
        return CurrentWorkHandoff.model_validate(payload)
