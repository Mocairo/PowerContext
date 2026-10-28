"""The learner background assessment (FR-PLN-001, spec 14).

One of these records what the incoming learner can already do and what they came for. Spec 14 is
explicit that it is **not** a grade: the fields below exist so the planner can decide whether to
add foundational tasks, how coarse a stage should be and how much time to budget — never to rank
a person. That is why the derived values are minutes and boolean needs, not a score.

The four derived members are :func:`computed_field`, so they travel inside
``assessment.model_dump(mode="json")`` and reach the generation prompt without the planner having
to spell them out. They are ignored when a stored row is read back with ``model_validate``, which
is also why this model keeps pydantic's default ``extra="ignore"``: the repository stores the dump
and the service reads that same dump.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field

Level = Literal["none", "basic", "advanced"]
PaperLevel = Literal["none", "read_related", "detailed"]
ReproductionLevel = Literal["none", "basic", "experienced"]
Target = Literal["understand", "reproduce_baseline", "continue_experiment"]

#: A weekly budget outside these bounds is a data mistake, not a study plan (spec 14).
MIN_HOURS_PER_WEEK = 1
MAX_HOURS_PER_WEEK = 80

#: Minutes of paper work a *first* pass is given, by how deeply it has been read before.
PAPER_MINUTES: dict[str, int] = {"none": 90, "read_related": 45, "detailed": 20}

#: Minutes of environment and data work, by reproduction experience with a similar repo.
ENVIRONMENT_MINUTES: dict[str, int] = {"none": 90, "basic": 45, "experienced": 20}


class Assessment(BaseModel):
    id: str
    project_id: str
    python_level: Level
    linux_server_level: Level
    paper_level: PaperLevel
    reproduction_level: ReproductionLevel
    hours_per_week: int = Field(ge=MIN_HOURS_PER_WEEK, le=MAX_HOURS_PER_WEEK)
    target: Target

    @computed_field  # type: ignore[prop-decorator]
    @property
    def needs_foundations(self) -> bool:
        """Stage 1 and 3 need a Python warm-up when the learner has never written any."""
        return self.python_level == "none"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def needs_operations_training(self) -> bool:
        """The server rules cannot be assumed known, so stage 0 grows an operations drill."""
        return self.linux_server_level == "none"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def paper_depth(self) -> int:
        return PAPER_MINUTES[self.paper_level]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def environment_minutes(self) -> int:
        return ENVIRONMENT_MINUTES[self.reproduction_level]


def new_assessment_id() -> str:
    return f"assessment-{uuid.uuid4().hex}"


def build_assessment(payload: Mapping[str, Any]) -> Assessment:
    """Turn one answer set into an :class:`Assessment`, minting an id only if none was given.

    The service always passes the identity it owns (a fresh ``id`` plus the project the URL
    named), so a caller cannot pick which row an assessment writes onto. The default exists for
    the callers that have no project to attach to yet — a fixture or a dry run.
    """
    data: dict[str, Any] = dict(payload)
    data.setdefault("id", new_assessment_id())
    return Assessment.model_validate(data)
