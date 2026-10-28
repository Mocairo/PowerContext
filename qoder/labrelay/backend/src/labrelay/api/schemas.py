"""Request bodies for the Task 6 and Task 10 API.

The schemas are deliberately thin: they decide what a caller may send, and nothing else. Domain
rules stay where they already live — the asset kinds are the ``AssetKind`` literal in
:mod:`labrelay.domain.models`, so an unknown kind is a 422 validation failure rather than a
silently empty search — and every failure a route raises is described by a code in
:mod:`labrelay.api.errors`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from labrelay.domain.assessment import (
    MAX_HOURS_PER_WEEK,
    MIN_HOURS_PER_WEEK,
    Level,
    PaperLevel,
    ReproductionLevel,
    Target,
)
from labrelay.domain.models import AssetKind

# ``configs/retrieval.yaml`` owns the result budget; the pinned test keeps the two in step.
DEFAULT_SEARCH_LIMIT = 12

#: Every identifier column in ``src/labrelay/seekdb/schema.sql`` is a VARCHAR(64), so a body
#: field that names a stored row is capped at the same width instead of failing at the store.
MAX_REFERENCE_ID_LENGTH = 64


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
