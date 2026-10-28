"""Deterministic stand-in for the model gateway used in tests and offline demos.

The plan is derived from the ``stages`` list the planner (Task 10) puts into the
payload — each stage carries its own ``required_evidence`` kinds, so this fake reads
the requirement from the request instead of hard-coding a stage table or a canned
``expected_plan`` answer.

Task ids are namespaced by the plan they are generated for (falling back to the assessment, then
to a digest of the request): two plan versions of one project must never write onto the same
``learning_task.id`` primary key, and a prerequisite chain has to point at the tasks of its own
version.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: Texts hinting at hands-on work get a stronger vector than pure reading.
ACTIVE_TERMS = ("baseline", "训练", "实验")

#: ``learning_task.id`` is a VARCHAR(64); leaving room for the ``-stage-<n>`` suffix.
MAX_NAMESPACE_CHARS = 48


def _named(value: object) -> str:
    """A usable namespace taken from one payload field, or the empty string."""
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _task_namespace(user_payload: dict[str, object]) -> str:
    """Stable discriminator for the ids of one plan, from the payload itself.

    The plan being generated wins, then the assessment it was planned from, then a digest of the
    request: a caller that supplies neither still cannot collide with another one's ids.
    """
    assessment = user_payload.get("assessment")
    discriminator = _named(user_payload.get("plan_id")) or _named(
        assessment.get("id") if isinstance(assessment, dict) else None
    )
    if not discriminator:
        # Payloads without either id (fixtures, older callers) still must not share an id
        # space, so fall back to a digest of the request.
        canonical = json.dumps(user_payload, sort_keys=True, ensure_ascii=False, default=str)
        discriminator = f"payload-{hashlib.sha256(canonical.encode()).hexdigest()[:16]}"
    if len(discriminator) > MAX_NAMESPACE_CHARS:
        digest = hashlib.sha256(discriminator.encode()).hexdigest()[:15]
        discriminator = f"{discriminator[: MAX_NAMESPACE_CHARS - 16]}-{digest}"
    return discriminator


class DeterministicModelGateway:
    def __init__(self, dimension: int = 2) -> None:
        self.dimension = dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        # Every component of one text repeats a single value, so the vectors are a
        # deterministic *degenerate* stand-in: they keep Task 7's plumbing runnable but
        # say nothing about retrieval quality. Never read results produced with them as
        # a search benchmark.
        return [
            [1.0 if any(term in text.lower() for term in ACTIVE_TERMS) else 0.5] * self.dimension
            for text in texts
        ]

    async def generate_json(
        self, system_prompt: str, user_payload: dict[str, object]
    ) -> dict[str, object]:
        stages = user_payload.get("stages") if isinstance(user_payload, dict) else None
        if not isinstance(stages, list):
            raise ValueError("model payload must contain a 'stages' list")

        namespace = _task_namespace(user_payload)
        tasks: list[dict[str, Any]] = []
        previous_id: str | None = None
        for stage in stages:
            stage_id = f"{namespace}-stage-{stage['id']}"
            evidence = stage["evidence"]
            required = set(stage["required_evidence"])
            present = {item["citation"]["kind"] for item in evidence}
            needs_material = stage["id"] == 4 and not required.issubset(present)
            if evidence:
                citation = (
                    [item["citation"] for item in evidence]
                    if stage["id"] == 4
                    else [evidence[0]["citation"]]
                )
                task_type = "citation_required"
            else:
                citation = []
                task_type = "self_assessment"
            tasks.append(
                {
                    "id": stage_id,
                    "stage": stage["id"],
                    "title": f"阶段 {stage['id']}",
                    "objective": f"完成阶段 {stage['id']} 的学习目标",
                    "prerequisite_task_ids": [previous_id] if previous_id else [],
                    "estimated_minutes": 30,
                    "citations": citation,
                    "steps": ["查看当前阶段资料并提交结果"],
                    "pitfalls": [],
                    "completion_criteria": "提交可核对结果",
                    "evidence_requirements": ["summary"],
                    "status": "needs_material" if needs_material else "locked",
                    "task_type": task_type,
                }
            )
            previous_id = stage_id
        return {"tasks": tasks}
