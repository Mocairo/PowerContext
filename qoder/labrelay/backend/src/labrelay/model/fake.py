"""Deterministic stand-in for the model gateway used in tests and offline demos.

The plan is derived from the ``stages`` list the planner (Task 10) puts into the
payload — each stage carries its own ``required_evidence`` kinds, so this fake reads
the requirement from the request instead of hard-coding a stage table or a canned
``expected_plan`` answer.
"""

from __future__ import annotations

from typing import Any

#: Texts hinting at hands-on work get a stronger vector than pure reading.
ACTIVE_TERMS = ("baseline", "训练", "实验")


class DeterministicModelGateway:
    def __init__(self, dimension: int = 2) -> None:
        self.dimension = dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [
            [
                1.0 if any(term in text.lower() for term in ACTIVE_TERMS) else 0.5
            ]
            * self.dimension
            for text in texts
        ]

    async def generate_json(
        self, system_prompt: str, user_payload: dict[str, object]
    ) -> dict[str, object]:
        stages = user_payload.get("stages") if isinstance(user_payload, dict) else None
        if not isinstance(stages, list):
            raise ValueError("model payload must contain a 'stages' list")

        tasks: list[dict[str, Any]] = []
        previous_id: str | None = None
        for stage in stages:
            stage_id = f"stage-{stage['id']}"
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
