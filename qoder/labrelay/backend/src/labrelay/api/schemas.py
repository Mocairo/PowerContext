"""Request bodies for the Task 6 API.

The schemas are deliberately thin: they decide what a caller may send, and nothing else. Domain
rules stay where they already live — the asset kinds are the ``AssetKind`` literal in
:mod:`labrelay.domain.models`, so an unknown kind is a 422 validation failure rather than a
silently empty search — and every failure a route raises is described by a code in
:mod:`labrelay.api.errors`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from labrelay.domain.models import AssetKind

# ``configs/retrieval.yaml`` owns the result budget; the pinned test keeps the two in step.
DEFAULT_SEARCH_LIMIT = 12


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
