"""Request bodies for the Task 6 and Task 10 API.

The schemas are deliberately thin: they decide what a caller may send, and nothing else. Domain
rules stay where they already live — the asset kinds are the ``AssetKind`` literal in
:mod:`labrelay.domain.models`, so an unknown kind is a 422 validation failure rather than a
silently empty search — and every failure a route raises is described by a code in
:mod:`labrelay.api.errors`.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from labrelay.domain.assessment import (
    MAX_HOURS_PER_WEEK,
    MIN_HOURS_PER_WEEK,
    Level,
    PaperLevel,
    ReproductionLevel,
    Target,
)
from labrelay.domain.models import AssetKind, TaskStatus

# ``configs/retrieval.yaml`` owns the result budget; the pinned test keeps the two in step.
DEFAULT_SEARCH_LIMIT = 12

#: Every identifier column in ``src/labrelay/seekdb/schema.sql`` is a VARCHAR(64), so a body
#: field that names a stored row is capped at the same width instead of failing at the store.
MAX_REFERENCE_ID_LENGTH = 64

#: A completion is a small record, not an upload. Bounding the open mapping keeps one request from
#: writing an unbounded append-only TaskEvent.
MAX_EVIDENCE_BYTES = 64 * 1024
MAX_EVIDENCE_KEYS = 32


class CreateProjectRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    # A PowerContext scope title is ``{title}:project:{project_id}`` with a 256-char limit,
    # so the human title must leave room for that suffix.
    title: str = Field(min_length=1, max_length=180)
    research_area: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=2000)


class ImportRequest(BaseModel):
    # An empty path would resolve to the server's own working directory, so "which folder" is
    # required by the schema instead of being guessed by the route.
    path: str = Field(min_length=1)


class SearchRequest(BaseModel):
    query: str
    # Typed as the domain literal, so a kind the importer can never produce is a 422 naming the
    # field rather than a search that quietly answers with nothing.
    asset_kinds: list[AssetKind] = []
    limit: int = DEFAULT_SEARCH_LIMIT


class AssessmentRequest(BaseModel):
    """The intake answers of spec 14, typed by the very literals the domain uses.

    ``id`` and ``project_id`` are deliberately absent: the service mints both from the URL it was
    called with, so a caller cannot choose which row its answers are filed under. The weekly
    bounds come from :mod:`labrelay.domain.assessment` rather than being repeated here, which is
    what keeps a 422 and a rejected ``Assessment`` saying the same thing.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    python_level: Level
    linux_server_level: Level
    paper_level: PaperLevel
    reproduction_level: ReproductionLevel
    hours_per_week: int = Field(ge=MIN_HOURS_PER_WEEK, le=MAX_HOURS_PER_WEEK)
    target: Target


class GeneratePlanRequest(BaseModel):
    """Generate the next plan version for one project from one of its assessments."""

    model_config = ConfigDict(str_strip_whitespace=True)

    assessment_id: str = Field(min_length=1, max_length=MAX_REFERENCE_ID_LENGTH)


class TaskUpdateRequest(BaseModel):
    """One learner-authored progress write: the new status plus whatever proves it.

    ``status`` is the domain literal, so a status the model can never hold (``archived``) is a 422
    naming the field rather than a transition the service has to refuse. ``evidence`` is
    deliberately an open mapping: what a completion needs is declared by the task itself
    (``LearningTask.evidence_requirements``), and the same body carries a skip's reason and
    confirmation. Which keys are required is Task 11's rule, checked against the stored task.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    status: TaskStatus
    evidence: dict[str, Any] | None = None

    @field_validator("evidence")
    @classmethod
    def bound_evidence(cls, evidence: dict[str, Any] | None) -> dict[str, Any] | None:
        if evidence is None:
            return None
        if len(evidence) > MAX_EVIDENCE_KEYS:
            raise ValueError(f"evidence may contain at most {MAX_EVIDENCE_KEYS} keys")
        encoded = json.dumps(evidence, ensure_ascii=False, default=str).encode("utf-8")
        if len(encoded) > MAX_EVIDENCE_BYTES:
            raise ValueError(f"evidence exceeds {MAX_EVIDENCE_BYTES} bytes")
        return evidence


class ReplanRequest(BaseModel):
    """Ask for a new plan version, saying why.

    The reason is required: it is the one part of a replan the system cannot infer, it travels in
    the model payload, and a path that changed for no recorded reason is the thing spec 16.2 rules
    out.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    reason: str = Field(min_length=1, max_length=2000)
