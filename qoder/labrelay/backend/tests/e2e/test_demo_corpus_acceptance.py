"""Acceptance for the demo corpus: one import has to carry the whole handoff path.

The corpus under ``examples/demo-project`` is what a judge or a new developer runs against, so
these tests read the real files rather than fixtures. They pin the three promises the corpus
README makes: every stage's evidence kinds are present and searchable, a plan generated from it
activates instead of stalling as a draft, and each verification question comes back with a
citation behind it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from labrelay.application.qa import answer_project_question
from labrelay.application.services import LabRelayService
from labrelay.context.memory import InMemoryContextGateway
from labrelay.model.fake import DeterministicModelGateway
from labrelay.persistence.memory import InMemoryRepository
from labrelay.planning.stages import STAGE_REQUIRED_KINDS
from labrelay.retrieval.in_memory import InMemorySearchEngine

#: ``tests/e2e/<this file>`` -> backend -> labrelay, which holds ``examples/``.
DEMO_CORPUS = Path(__file__).resolve().parents[3] / "examples" / "demo-project"

#: The six questions the corpus README promises to answer.
VERIFICATION_QUESTIONS = (
    "如何复现 baseline？",
    "实验配置在哪里？",
    "历史指标是多少？",
    "已知数据缓存问题是什么？",
    "下一项未完成工作是什么？",
    "实验室资源应如何申请和释放？",
)

#: The credential canary is a placeholder, but it must still be withheld from the index.
CANARY_TITLE = "credential_scan_demo.md"


async def make_demo_service() -> tuple[LabRelayService, str, dict[str, object]]:
    repository = InMemoryRepository()
    await repository.initialize()
    gateway = DeterministicModelGateway()
    service = LabRelayService(
        repository,
        InMemorySearchEngine(gateway),
        InMemoryContextGateway(),
        gateway,
    )
    project = await service.create_project(
        "Aurora 时序异常检测", "深度学习", "复现 baseline 并推进 seed 消融"
    )
    imported = await service.import_project_files(project.id, DEMO_CORPUS)
    return service, project.id, dict(imported)


async def test_demo_corpus_holds_every_stage_evidence_kind() -> None:
    assert DEMO_CORPUS.is_dir(), f"missing demo corpus at {DEMO_CORPUS}"
    service, project_id, imported = await make_demo_service()

    assert imported["failed"] == 0
    assert imported["skipped"] == 0
    assert imported["assets"]

    assets = await service.repository.list_assets(project_id)
    searchable_kinds = {asset.kind for asset in assets if asset.status == "active"}
    required = {kind for kinds in STAGE_REQUIRED_KINDS.values() for kind in kinds}
    assert required <= searchable_kinds, sorted(required - searchable_kinds)


async def test_demo_corpus_withholds_the_credential_canary_from_the_index() -> None:
    service, project_id, imported = await make_demo_service()
    canary = next(
        asset
        for asset in await service.repository.list_assets(project_id)
        if asset.title == CANARY_TITLE
    )

    assert canary.status == "restricted"
    assert canary.id in imported["restricted_reasons"]
    hits = await service.search(project_id, CANARY_TITLE, [], 12)
    assert canary.id not in {hit.citation.asset_id for hit in hits}


@pytest.mark.parametrize("question", VERIFICATION_QUESTIONS)
async def test_each_verification_question_is_evidence_backed(question: str) -> None:
    """No question may be answered from the model's own imagination, even on the offline gateway."""
    service, project_id, _ = await make_demo_service()
    response = await answer_project_question(service, project_id, question)

    assert response.citations, f"{question} returned no citation"
    assert all(citation.excerpt for citation in response.citations)


async def test_demo_corpus_generates_an_activatable_seven_stage_plan() -> None:
    service, project_id, _ = await make_demo_service()
    assessment = await service.submit_assessment(
        project_id,
        {
            "python_level": "basic",
            "linux_server_level": "none",
            "paper_level": "none",
            "reproduction_level": "none",
            "hours_per_week": 10,
            "target": "reproduce_baseline",
        },
    )
    plan = await service.generate_plan(project_id, assessment.id)

    assert plan.status == "active", [
        (task.stage, task.status) for task in plan.tasks if task.status == "needs_material"
    ]
    assert [task.stage for task in plan.tasks] == list(range(7))
    assert all(task.citations for task in plan.tasks)
    assert [task.status for task in plan.tasks] == ["todo"] + ["locked"] * 6
