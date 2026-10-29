"""Deterministic stand-in for the model gateway used in tests and offline demos.

The plan is derived from the ``stages`` list the planner (Task 10) puts into the
payload — each stage carries its own ``required_evidence`` kinds, so this fake reads
the requirement from the request instead of hard-coding a stage table or a canned
``expected_plan`` answer.

A question payload takes the same offline route: instead of raising, the stand-in quotes the
retrieved excerpts, so a demo without a model key still returns an evidence-backed answer.

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

#: How many hits the offline answer quotes; more would bury the caller in raw text.
MAX_QUOTED_HITS = 3

#: Says the text is untrusted project material, not a model conclusion, so an excerpt that
#: reads like an instruction is not mistaken for one.
OFFLINE_ANSWER_PREFIX = "以下为项目资料原文摘录（离线确定性模型，未做推理）："
NO_EVIDENCE_ANSWER = "无法从项目资料确认。"

#: Same disclosure as the answer path, plus a cap on how much request text is echoed.
OFFLINE_CHECK_HEADLINE = "离线确定性模型：没有推理，只回显本次请求"
MAX_CHECK_ECHO_CHARS = 200


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
        if isinstance(user_payload, dict) and "question" in user_payload:
            return self._answer_question(user_payload)
        if isinstance(user_payload, dict) and "prompt" in user_payload:
            return self._check_prompt(user_payload)

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

    def _answer_question(self, user_payload: dict[str, object]) -> dict[str, object]:
        """Echo the retrieved excerpts, because an offline demo has no model to reason with.

        The answer is quoted source text, never a conclusion, and ``used_asset_ids`` only names
        assets that were actually in the evidence, so :mod:`labrelay.application.qa` can still
        attach citations the caller is allowed to see.
        """
        evidence = user_payload.get("evidence")
        hits = evidence if isinstance(evidence, list) else []
        quoted: list[str] = []
        used: list[str] = []
        for item in hits[:MAX_QUOTED_HITS]:
            citation = item.get("citation") if isinstance(item, dict) else None
            if not isinstance(citation, dict):
                continue
            asset_id = citation.get("asset_id")
            if not isinstance(asset_id, str) or not asset_id:
                continue
            title = str(citation.get("title") or asset_id)
            excerpt = " ".join(str(citation.get("excerpt") or "").split())
            quoted.append(f"《{title}》摘录：{excerpt}" if excerpt else f"《{title}》")
            used.append(asset_id)
        if not used:
            return {"answer": NO_EVIDENCE_ANSWER, "sufficient": False, "used_asset_ids": []}
        return {
            "answer": f"{OFFLINE_ANSWER_PREFIX}" + "；".join(quoted),
            "sufficient": True,
            "used_asset_ids": used,
        }

    def _check_prompt(self, user_payload: dict[str, object]) -> dict[str, object]:
        """Answer the panel's model probe by echoing the request; there is nothing to reason with.

        A canned "先核对数据缓存" headline would have read like a model conclusion drawn from this
        project's evidence; the offline gateway has no such understanding, so it says so instead.
        """
        prompt = user_payload.get("prompt")
        text = prompt.strip() if isinstance(prompt, str) else ""
        return {
            "headline": OFFLINE_CHECK_HEADLINE,
            "next_step": f"请求原文：{text[:MAX_CHECK_ECHO_CHARS]}"
            if text
            else "请求未带 prompt。",
        }
