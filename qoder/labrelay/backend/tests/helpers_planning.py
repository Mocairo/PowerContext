"""Planning fixtures shared by Task 10's unit and API tests.

Everything here is a double for one of the four ports :class:`LabRelayService` is built on, plus
the real :class:`InMemoryRepository`. The doubles stay deliberately dumb: they record what the
planner asked for so a test can assert on the *sequence* of calls (which stage got which kinds,
which payload reached the model, which note landed in learner memory) instead of on a canned
plan object.

The asset kinds come from :data:`ASSET_KIND_BY_ID` rather than being typed per test, because
``validate_plan`` refuses a citation whose kind differs from the asset it names — a fixture with
a wrong kind would fail for the wrong reason.
"""

from __future__ import annotations

from typing import Any

from labrelay.application.services import LabRelayService
from labrelay.config import PlannerConfig
from labrelay.domain.models import Asset, Citation, Project
from labrelay.domain.ports import SearchHit
from labrelay.persistence.memory import InMemoryRepository

PROJECT_ID = "project-1"

#: The kind every fixture asset of a given id carries. Keep in sync with :func:`asset`.
ASSET_KIND_BY_ID: dict[str, str] = {
    "code-1": "code",
    "config-1": "config",
    "experiment-1": "experiment",
    "note-1": "note",
    "note-2": "note",
    "ops-1": "operations",
    "paper-1": "paper",
}


def asset(asset_id: str, kind: str, *, project_id: str = PROJECT_ID) -> Asset:
    return Asset(
        id=asset_id,
        project_id=project_id,
        kind=kind,  # type: ignore[arg-type]
        title=f"{asset_id}.{kind}",
        uri=f"repo/{asset_id}",
        checksum=asset_id,
        version="commit-1",
        status="active",
    )


def make_assets() -> list[Asset]:
    return [
        asset(asset_id, kind)
        for asset_id, kind in [
            ("code-1", "code"),
            ("config-1", "config"),
            ("experiment-1", "experiment"),
            ("note-1", "note"),
            ("ops-1", "operations"),
        ]
    ]


def citation(asset_id: str, kind: str | None = None, *, title: str | None = None) -> Citation:
    resolved_kind = kind if kind is not None else ASSET_KIND_BY_ID[asset_id]
    return Citation(
        asset_id=asset_id,
        kind=resolved_kind,  # type: ignore[arg-type]
        title=title or asset_id,
        uri=f"repo/{title or asset_id}",
        version="commit-1",
        locator={"line_start": 1, "line_end": 10},
        excerpt="evidence",
    )


def citation_payload(asset_id: str, kind: str | None = None) -> dict[str, Any]:
    return citation(asset_id, kind).model_dump(mode="json")


def task_payload(
    task_id: str,
    stage: int,
    required_asset: str,
    prerequisites: tuple[str, ...] = (),
    **overrides: Any,
) -> dict[str, Any]:
    """One model-shaped task record: the planner adds ``project_id`` and ``plan_id``."""
    payload: dict[str, Any] = {
        "id": task_id,
        "stage": stage,
        "title": f"任务 {task_id}",
        "objective": "完成该阶段",
        "prerequisite_task_ids": list(prerequisites),
        "estimated_minutes": 30,
        "citations": [citation_payload(required_asset)],
        "steps": ["按资料执行"],
        "pitfalls": [],
        "completion_criteria": "提交结果",
        "evidence_requirements": ["summary"],
        "task_type": "citation_required",
    }
    payload.update(overrides)
    return payload


def baseline_task_payload(task_id: str = "baseline", prerequisites: tuple[str, ...] = ()) -> dict:
    """A stage-4 task carrying the code, config and experiment evidence FR-PLN-006 demands."""
    return task_payload(
        task_id,
        4,
        "code-1",
        prerequisites,
        title="复现 baseline",
        objective="复现实验",
        estimated_minutes=120,
        citations=[
            citation_payload("code-1"),
            citation_payload("config-1"),
            citation_payload("experiment-1"),
        ],
        steps=["使用 baseline 配置运行训练"],
        pitfalls=["检查数据缓存"],
        completion_criteria="指标可解释",
        evidence_requirements=["command_summary", "metrics_comparison"],
    )


def full_plan_tasks() -> list[dict[str, Any]]:
    """A seven-stage plan whose evidence is complete, so it is expected to activate."""
    return [
        task_payload("orientation", 0, "ops-1"),
        task_payload("paper", 1, "note-1", ("orientation",)),
        task_payload("environment", 2, "config-1", ("orientation",)),
        task_payload("code", 3, "code-1", ("environment",)),
        baseline_task_payload("baseline", ("code",)),
        task_payload("next", 5, "code-1", ("baseline",)),
        task_payload("summary", 6, "note-1", ("next",)),
    ]


def valid_assessment_payload() -> dict[str, Any]:
    return {
        "python_level": "basic",
        "linux_server_level": "none",
        "paper_level": "none",
        "reproduction_level": "none",
        "hours_per_week": 10,
        "target": "reproduce_baseline",
    }


def advanced_assessment_payload() -> dict[str, Any]:
    return {
        "python_level": "advanced",
        "linux_server_level": "basic",
        "paper_level": "detailed",
        "reproduction_level": "basic",
        "hours_per_week": 10,
        "target": "reproduce_baseline",
    }


class FakeContext:
    """``ContextGateway`` double recording every note and handing back one handoff string."""

    def __init__(self, handoff_text: str | None = None) -> None:
        self.handoff_text = handoff_text
        self.remembered: list[tuple[str, str, str, str | None]] = []
        self.prepared: list[tuple[str, str, int]] = []

    async def ensure_scopes(self, project: Project) -> tuple[str, str]:
        return f"project:{project.id}", f"learner:{project.id}"

    async def remember(
        self, scope_id: str, kind: str, text: str, reason: str | None = None
    ) -> None:
        self.remembered.append((scope_id, kind, text, reason))

    async def prepare_context(
        self, scope_id: str, query: str, max_bytes: int
    ) -> tuple[str | None, str | None]:
        self.prepared.append((scope_id, query, max_bytes))
        if self.handoff_text is None:
            return None, None
        return "ready", self.handoff_text

    async def project_context_summary(
        self, project_scope_id: str, learner_scope_id: str, query: str
    ) -> tuple[str | None, str]:
        del query
        return None, "local_progress"

    async def health_check(self) -> bool:
        return True


class FakeSearch:
    """``SearchEngine`` double: every asset of a requested kind is a hit, score fixed at 1.0."""

    def __init__(self, assets: list[Asset]) -> None:
        self._assets = assets
        self.calls: list[tuple[str, str, list[str], int]] = []

    async def upsert_chunks(self, chunks, assets) -> None:
        del chunks, assets
        return None

    async def search(
        self, project_id: str, query: str, asset_kinds: list[str], limit: int
    ) -> list[SearchHit]:
        self.calls.append((project_id, query, list(asset_kinds), limit))
        candidates = [item for item in self._assets if not asset_kinds or item.kind in asset_kinds]
        return [
            SearchHit(
                citation=Citation(
                    asset_id=item.id,
                    kind=item.kind,
                    title=item.title,
                    uri=item.uri,
                    version="commit-1",
                    locator={"line_start": 1, "line_end": 20},
                    excerpt="evidence",
                ),
                score=1.0,
            )
            for item in candidates[:limit]
        ]


class ModelReturning:
    """``ModelGateway`` double replaying one canned answer and keeping the payloads it got."""

    dimension = 2

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    async def generate_json(
        self, system_prompt: str, user_payload: dict[str, object]
    ) -> dict[str, object]:
        self.calls.append((system_prompt, dict(user_payload)))
        return self.response


async def make_service(
    *,
    assets: list[Asset],
    model_response: dict[str, Any] | None = None,
    model: Any | None = None,
    context: FakeContext | None = None,
    assets_are_searchable: bool = True,
    planner_config: PlannerConfig | None = None,
) -> tuple[LabRelayService, Project]:
    """A service on the real repository with fake engine, memory and model doubles."""
    repository = InMemoryRepository()
    await repository.initialize()
    project = Project(
        id=PROJECT_ID,
        title="Aurora",
        research_area="DL",
        description="baseline",
        project_scope_id=f"project:{PROJECT_ID}",
        learner_scope_id=f"learner:{PROJECT_ID}",
    )
    await repository.save_project(project)
    for item in assets:
        await repository.save_asset(item)
    searchable = assets if assets_are_searchable else []
    if model is None and model_response is None:
        raise ValueError("make_service needs either a canned model_response or a model double")
    service = LabRelayService(
        repository,
        FakeSearch(searchable),
        context if context is not None else FakeContext(),
        model if model is not None else ModelReturning(dict(model_response or {})),
        planner_config if planner_config is not None else PlannerConfig(),
    )
    return service, project
