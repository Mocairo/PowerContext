from types import SimpleNamespace

from powercontext.http import (
    AcknowledgeHandoffRequest,
    ArtifactReference,
    CommitHandoffRequest,
    ContinueHandoffRequest,
    CreateScopeRequest,
    CreateSourceRequest,
    CreateWorkContractRequest,
    CurrentWorkHandoff,
    HandoffAcknowledgementSelection,
    HandoffContent,
    HandoffCurrentWorkRequest,
    HandoffReceiptStatus,
    PreparedContextStatus,
    PreparedHandoff,
    RecordTaskOutcomeRequest,
    TaskOutcome,
    WorkContract,
)

from labrelay.context.memory import InMemoryContextGateway
from labrelay.context.powercontext import PowerContextService
from labrelay.domain.models import Project


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.liveness_fails = False

    async def create_scope(self, request: CreateScopeRequest) -> SimpleNamespace:
        self.calls.append(("create_scope", request))
        return SimpleNamespace(scope_id=f"scope:{request.idempotency_key}")

    async def remember_memory(self, request) -> None:
        self.calls.append(("remember_memory", request))

    async def prepare_context(self, request) -> SimpleNamespace:
        self.calls.append(("prepare_context", request))
        return SimpleNamespace(status=PreparedContextStatus.READY, content="history")

    async def get_liveness(self) -> SimpleNamespace:
        if self.liveness_fails:
            raise RuntimeError("powercontext is down")
        return SimpleNamespace(status="alive")

    async def create_source(self, scope_id: str, request: CreateSourceRequest) -> SimpleNamespace:
        self.calls.append(("create_source", (scope_id, request)))
        return SimpleNamespace(source_id="source-1")

    async def create_work_contract(self, request: CreateWorkContractRequest) -> SimpleNamespace:
        self.calls.append(("create_work_contract", request))
        return SimpleNamespace(kind="work-contract")

    async def handoff_current_work(self, request: HandoffCurrentWorkRequest) -> SimpleNamespace:
        self.calls.append(("handoff_current_work", request))
        return SimpleNamespace(handoff=object())

    async def commit_handoff(self, request: CommitHandoffRequest) -> SimpleNamespace:
        self.calls.append(("commit_handoff", request))
        return SimpleNamespace(
            reference=ArtifactReference(family="handoff", artifact_id="h1", revision=1)
        )

    async def continue_handoff(self, request: ContinueHandoffRequest) -> SimpleNamespace:
        self.calls.append(("continue_handoff", request))
        return SimpleNamespace(content=object())

    async def acknowledge_handoff(self, request: AcknowledgeHandoffRequest) -> SimpleNamespace:
        self.calls.append(("acknowledge_handoff", request))
        return SimpleNamespace(receipt=object())

    async def record_task_outcome(self, request: RecordTaskOutcomeRequest) -> SimpleNamespace:
        self.calls.append(("record_task_outcome", request))
        return SimpleNamespace(source=object())


def make_project() -> Project:
    return Project(
        id="project-1",
        title="Aurora",
        research_area="DL",
        description="baseline",
        project_scope_id="",
        learner_scope_id="",
    )


async def test_ensure_scopes_creates_stable_project_and_learner_scopes() -> None:
    client = RecordingClient()
    service = PowerContextService(client)

    first = await service.ensure_scopes(make_project())
    second = await service.ensure_scopes(make_project())

    assert first == (
        "scope:labrelay:project:project-1",
        "scope:labrelay:learner:project-1",
    )
    assert second == first
    requests = [call[1] for call in client.calls if call[0] == "create_scope"]
    assert len(requests) == 4
    assert requests[0].title == "Aurora:project:project-1"
    assert requests[0].idempotency_key == "labrelay:project:project-1"
    assert requests[1].title == "Aurora:learner:project-1"
    assert requests[1].idempotency_key == "labrelay:learner:project-1"
    assert requests[0].summary


async def test_remember_sends_a_memory_request() -> None:
    client = RecordingClient()
    service = PowerContextService(client)

    await service.remember("scope-1", "decision", "use uv", reason="project setup")

    request = client.calls[-1][1]
    assert request.scope_id == "scope-1"
    assert request.kind == "decision"
    assert request.text == "use uv"
    assert request.reason == "project setup"


async def test_prepare_context_returns_status_and_content() -> None:
    client = RecordingClient()
    service = PowerContextService(client)

    result = await service.prepare_context("scope-1", "baseline", 4096)

    assert result == ("ready", "history")
    request = client.calls[-1][1]
    assert request.scope_id == "scope-1"
    assert request.query == "baseline"
    assert request.max_bytes == 4096


async def test_project_context_summary_combines_both_scopes() -> None:
    client = RecordingClient()
    service = PowerContextService(client)

    content, source = await service.project_context_summary(
        "project-scope", "learner-scope", "baseline"
    )

    assert content == "history\nhistory"
    assert source == "powercontext"
    scopes = [call[1].scope_id for call in client.calls if call[0] == "prepare_context"]
    assert scopes == ["project-scope", "learner-scope"]


async def test_health_check_reflects_client_failure() -> None:
    client = RecordingClient()
    service = PowerContextService(client)

    assert await service.health_check() is True
    client.liveness_fails = True
    assert await service.health_check() is False


async def test_create_source_and_contract_delegate() -> None:
    client = RecordingClient()
    service = PowerContextService(client)
    source_request = CreateSourceRequest(content={"objective": "baseline"})
    contract = WorkContract.model_validate(
        {
            "schema": "powercontext.work-contract.v1",
            "trust": "untrusted_input",
            "objective": "baseline",
            "facts": [],
            "in_scope": ["reproduce baseline"],
            "exclusions": [],
            "completion_criteria": ["baseline metrics match"],
            "authorization_notes": [],
            "open_questions": [],
        }
    )
    contract_request = CreateWorkContractRequest(
        scope_id="scope-1", source_id="source-1", contract=contract
    )

    source = await service.create_source("scope-1", source_request)
    receipt = await service.create_contract(contract_request)

    assert source.source_id == "source-1"
    assert receipt.kind == "work-contract"
    assert client.calls[-2] == ("create_source", ("scope-1", source_request))
    assert client.calls[-1] == ("create_work_contract", contract_request)


async def test_handoff_requests_delegate_with_exact_revision() -> None:
    client = RecordingClient()
    service = PowerContextService(client)
    current_work = CurrentWorkHandoff.model_validate(
        {
            "schema": "powercontext.current-work-handoff.v1",
            "trust": "untrusted_input",
            "objective": "baseline",
            "state": [{"text": "baseline trained", "basis": "declared", "evidence": []}],
            "disposition": "continuable",
            "next_action": None,
            "omissions": [],
        }
    )
    handoff_request = HandoffCurrentWorkRequest(
        scope_id="scope-1", source_id="source-1", handoff=current_work
    )
    handoff_content = HandoffContent.model_validate(
        {
            "schema": "powercontext.handoff.v1",
            "objective": "baseline",
            "state": [
                {
                    "text": "baseline trained",
                    "citations": [
                        {
                            "kind": "source",
                            "source_ref": {
                                "name": "handoff-source",
                                "source_id": "source-1",
                            },
                        }
                    ],
                }
            ],
            "disposition": "continuable",
            "next_action": None,
            "omissions": [],
        }
    )
    prepared = PreparedHandoff.model_validate(
        {
            "schema": "powercontext.prepared-handoff.v1",
            "scope_id": "scope-1",
            "base": None,
            "content": handoff_content,
        }
    )
    commit_request = CommitHandoffRequest(scope_id="scope-1", handoff=prepared)
    reference = ArtifactReference(family="handoff", artifact_id="h1", revision=1)
    continue_request = ContinueHandoffRequest(
        scope_id="scope-1",
        selection=HandoffAcknowledgementSelection.EXACT.value,
        revision=reference,
    )
    acknowledge_request = AcknowledgeHandoffRequest(
        scope_id="scope-1",
        source_id="source-ack",
        receiver="new-student",
        status=HandoffReceiptStatus.ACCEPTED,
        selection=HandoffAcknowledgementSelection.EXACT.value,
        revision=reference,
    )
    outcome = TaskOutcome.model_validate(
        {
            "schema": "powercontext.task-outcome.v1",
            "trust": "untrusted_observation",
            "objective": "baseline",
            "status": "succeeded",
            "summary": "reproduced",
            "observations": [{"text": "baseline reproduced", "basis": "declared", "evidence": []}],
            "checks": [],
            "produced_artifacts": [],
            "remaining_work": [],
        }
    )
    outcome_request = RecordTaskOutcomeRequest(
        scope_id="scope-1", source_id="source-outcome", outcome=outcome
    )

    prepared_result = await service.prepare_handoff(handoff_request)
    committed = await service.commit_handoff(commit_request)
    resolved = await service.continue_handoff(continue_request)
    acknowledged = await service.acknowledge_handoff(acknowledge_request)
    recorded = await service.record_outcome(outcome_request)

    assert prepared_result.handoff is not None
    assert committed.reference.artifact_id == "h1"
    assert resolved.content is not None
    assert acknowledged.receipt is not None
    assert recorded.source is not None
    assert client.calls[-5:] == [
        ("handoff_current_work", handoff_request),
        ("commit_handoff", commit_request),
        ("continue_handoff", continue_request),
        ("acknowledge_handoff", acknowledge_request),
        ("record_task_outcome", outcome_request),
    ]


async def test_in_memory_gateway_stores_scopes_memories_and_context() -> None:
    gateway = InMemoryContextGateway()
    project = make_project()

    first = await gateway.ensure_scopes(project)
    second = await gateway.ensure_scopes(project)
    assert first == second
    assert first[0] != first[1]

    await gateway.remember(first[0], "decision", "use uv", reason="setup")
    await gateway.remember(first[1], "progress", "finished stage 1")
    status, content = await gateway.prepare_context(first[0], "uv", 4096)
    summary, source = await gateway.project_context_summary(first[0], first[1], "progress")

    assert status == "ready"
    assert content == "use uv"
    assert summary == "use uv\nfinished stage 1"
    assert source == "in_memory"
    assert await gateway.health_check() is True
    assert await gateway.prepare_context("unknown", "x", 4096) == (None, None)
