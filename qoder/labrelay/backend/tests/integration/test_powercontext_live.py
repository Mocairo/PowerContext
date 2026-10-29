"""Live end-to-end against a real PowerContext Server (skipped unless LABRELAY_POWERCONTEXT_TEST).

This is the evidence that the handoff chain is not only correct against the in-memory double:
every scope, memory, Source, Work Contract, Prepared/Committed Handoff, acknowledgement receipt
and task outcome below is created and read back over HTTP by a running PowerContext Server.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from labrelay.application.handoff import (
    HandoffAcknowledgementRequest,
    HandoffDraft,
    HandoffService,
    HandoffStateItem,
    ReceiverCheckValues,
    TaskOutcomeRequest,
)
from labrelay.config import LabRelayConfig
from labrelay.context.powercontext import PowerContextService
from labrelay.domain.models import Citation, Project
from labrelay.persistence.memory import InMemoryRepository

pytestmark = pytest.mark.skipif(
    not os.getenv("LABRELAY_POWERCONTEXT_TEST"),
    reason="set LABRELAY_POWERCONTEXT_TEST=1 with a live PowerContext Server",
)


def _config() -> LabRelayConfig:
    return LabRelayConfig(
        powercontext_url=os.getenv("POWERCONTEXT_URL", "http://127.0.0.1:8100"),
        powercontext_token=os.getenv("POWERCONTEXT_TOKEN") or None,
    )


def _project(suffix: str, project_scope: str, learner_scope: str) -> Project:
    return Project(
        id=f"project-{suffix}",
        title=f"Aurora live {suffix}",
        research_area="Deep learning",
        description="A synthetic project used to verify the live PowerContext handoff chain.",
        project_scope_id=project_scope,
        learner_scope_id=learner_scope,
    )


def _draft() -> HandoffDraft:
    citation = Citation(
        asset_id="asset-live",
        kind="code",
        title="train.py",
        uri="repo/train.py",
        version="deadbeef",
        locator={"line_start": 1, "line_end": 12},
    )
    return HandoffDraft(
        objective="复现 Aurora baseline 并交接给新人",
        state=[
            HandoffStateItem(
                text="baseline 已跑通，指标与论文一致",
                citations=[citation],
            ),
            HandoffStateItem(
                text="数据预处理脚本仍依赖一台未公开的 GPU 机器",
                citations=[citation],
            ),
        ],
        next_action="新人先读 train.py 的调用链，再在自有数据上复现一次",
        omissions=["未记录 GPU 机器的登录方式"],
        disposition="continuable",
    )


async def test_live_handoff_chain_round_trips_through_powercontext() -> None:
    config = _config()
    context = PowerContextService.from_config(config)
    assert await context.health_check(), "PowerContext Server is not reachable"

    suffix = uuid4().hex[:8]
    project = _project(suffix, "", "")
    project_scope, learner_scope = await context.ensure_scopes(project)
    assert project_scope and learner_scope
    assert project_scope != learner_scope
    project = project.model_copy(
        update={"project_scope_id": project_scope, "learner_scope_id": learner_scope}
    )

    repository = InMemoryRepository()
    await repository.save_project(project)

    await context.remember(
        project_scope, "decision", "选择 Aurora 作为 baseline 模型", "交接前记录"
    )
    status, content = await context.prepare_context(project_scope, "Aurora baseline", 8000)
    assert status == "ready"
    assert content and "Aurora" in content

    service = HandoffService(repository, context)
    preview = await service.prepare(project.id, _draft())
    assert preview.draft_id

    committed = await service.confirm(project.id, draft_id=preview.draft_id)
    assert committed.handoff_ref
    assert committed.content is not None
    assert committed.content["objective"] == "复现 Aurora baseline 并交接给新人"

    stored = await repository.get_project(project.id)
    assert stored is not None and stored.handoff_ref == committed.handoff_ref

    read_back = await service.get(project.id)
    assert read_back is not None
    assert read_back.handoff_ref == committed.handoff_ref
    assert read_back.content is not None
    assert read_back.content["next_action"]["text"] == preview.next_action

    acknowledged = await service.acknowledge(
        project.id,
        HandoffAcknowledgementRequest(
            receiver="new-student",
            status="accepted",
            receiver_checks=ReceiverCheckValues(
                live_state="confirmed", capability="confirmed", authorization="confirmed"
            ),
        ),
    )
    assert acknowledged.receipt_ref
    stored = await repository.get_project(project.id)
    assert stored is not None and stored.handoff_receipt_ref == acknowledged.receipt_ref

    outcome = await service.record_outcome(
        project.id,
        TaskOutcomeRequest(
            objective="复现 Aurora baseline",
            status="succeeded",
            summary="在新人自有数据上重跑，指标一致",
            observations=["预处理脚本可离线运行"],
            remaining_work=["补充 GPU 机器的访问说明"],
        ),
    )
    assert outcome is not None

    await context.client.aclose()
