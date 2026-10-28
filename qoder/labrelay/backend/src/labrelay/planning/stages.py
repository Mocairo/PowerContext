"""LabRelay's default seven-stage path (FR-PLN-003, spec 15.1).

The table lives here rather than inside the generator because two collaborators need the same
answer: the planner asks retrieval for a stage's evidence, and the validator decides whether that
stage actually got enough of it.

``configs/planner.yaml`` describes the same stages more loosely — it lists the evidence a stage is
*about*, not the evidence a task must cite to be handed to a learner. ``tests/unit/
test_plan_generation.py`` pins the relation that does hold: every kind the config names for a
stage is required here too, and the retrieval budget matches ``configs/retrieval.yaml``.
"""

from __future__ import annotations

#: The stages LabRelay plans through, in order (FR-PLN-003).
PLANNING_STAGES = range(7)

#: The result budget handed to one stage's retrieval call. Kept in step with
#: ``retrieval.fusion.output_limit`` in ``configs/retrieval.yaml``.
STAGE_EVIDENCE_LIMIT = 12

#: What to ask the evidence store for at each stage, in the vocabulary the project's own files
#: use. A stage query is deliberately concrete: 「资料在哪里」 is what a handoff note answers.
STAGE_QUERY_PREFIX: dict[int, str] = {
    0: "项目目标 数据位置 服务器 资源申请 资源释放 禁止事项",
    1: "研究问题 核心方法 公式 实验设置 结论",
    2: "环境安装 Python 依赖 数据版本 环境检查",
    3: "训练入口 评估入口 配置 数据加载 调用链",
    4: "baseline 配置 指标 随机种子 复现命令",
    5: "遗留问题 下一步 未完成实验 已知失败",
    6: "交接总结 已验证结果 剩余风险",
}

#: The asset kinds a stage must be able to cite before it can be handed to a learner. Stage 4 is
#: the strict one (FR-PLN-006): reproducing a baseline needs the code, the config that produced
#: the number and the experiment record that holds the number itself.
STAGE_REQUIRED_KINDS: dict[int, list[str]] = {
    0: ["operations", "note"],
    1: ["paper", "note"],
    2: ["code", "config", "operations"],
    3: ["code", "config"],
    4: ["code", "config", "experiment"],
    5: ["code", "experiment", "note"],
    6: ["note", "experiment"],
}


def stage_required_kinds(stage: int) -> list[str]:
    """The kinds stage ``stage`` must cite; a stage outside 0-6 is not a LabRelay stage."""
    return STAGE_REQUIRED_KINDS[stage]
