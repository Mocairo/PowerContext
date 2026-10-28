from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, model_validator

AssetKind = Literal["paper", "code", "config", "experiment", "note", "operations", "other"]
TaskStatus = Literal[
    "locked", "todo", "in_progress", "blocked", "done", "skipped", "needs_material"
]
PlanStatus = Literal["draft", "active", "superseded"]

ALLOWED_TASK_TRANSITIONS: dict[str, set[str]] = {
    "locked": {"todo"},
    "todo": {"in_progress", "blocked", "skipped", "needs_material"},
    "in_progress": {"blocked", "done", "skipped", "needs_material"},
    "blocked": {"in_progress", "skipped", "needs_material"},
    "needs_material": {"in_progress", "blocked"},
    "done": set(),
    "skipped": set(),
}


class Citation(BaseModel):
    asset_id: str
    kind: AssetKind
    title: str
    uri: str
    version: str | None = None
    locator: dict[str, str | int] = {}
    excerpt: str = ""


class Project(BaseModel):
    id: str
    title: str
    research_area: str
    description: str
    project_scope_id: str
    learner_scope_id: str
    active_plan_id: str | None = None
    handoff_ref: str | None = None


class Asset(BaseModel):
    id: str
    project_id: str
    kind: AssetKind
    title: str
    uri: str
    checksum: str
    version: str | None = None
    status: Literal["active", "restricted", "failed"]


class Chunk(BaseModel):
    id: str
    asset_id: str
    ordinal: int
    content: str
    locator: dict[str, str | int]
    token_count: int


class LearningTask(BaseModel):
    id: str
    project_id: str
    plan_id: str
    stage: int
    title: str
    objective: str
    prerequisite_task_ids: list[str] = []
    estimated_minutes: int = 30
    citations: list[Citation] = []
    steps: list[str] = []
    pitfalls: list[str] = []
    completion_criteria: str
    evidence_requirements: list[str] = ["summary"]
    status: TaskStatus = "locked"
    task_type: Literal["citation_required", "self_assessment"] = "citation_required"

    @model_validator(mode="after")
    def require_citation(self) -> LearningTask:
        if self.task_type == "citation_required" and not self.citations:
            raise ValueError("Learning task requires a citation")
        return self


class LearningPlan(BaseModel):
    id: str
    project_id: str
    version: int
    status: PlanStatus
    assessment_id: str
    tasks: list[LearningTask]


class TaskEvent(BaseModel):
    id: str
    project_id: str
    plan_id: str
    task_id: str
    event_type: str
    payload: dict[str, object]
    occurred_at: str
