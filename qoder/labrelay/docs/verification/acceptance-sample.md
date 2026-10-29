# 验收样例

对照技术规格第 24 节的 17 步操作，逐步给出「调哪个接口、期望看到什么」。
下面所有观测值都来自 2026-10-29 本地离线演示的真实运行（`scripts/smoke_api.py` 全绿 + 浏览器全流程），
不是手写预期。

## 快速复现

```bash
cd qoder/labrelay
# 终端 1
PYTHONPATH="$(pwd)/backend/src" LABRELAY_USE_FAKE_DEPS=1 \
  uv run --directory backend uvicorn labrelay.api.main:create_default_app \
  --factory --host 127.0.0.1 --port 8010
# 终端 2
npm --prefix frontend run dev
# 终端 3
uv run --directory backend python ../scripts/smoke_api.py http://127.0.0.1:8010
```

冒烟脚本每步输出 `PASS/FAIL`，末尾打印项目 id，任何一步失败都会以非 0 退出。

## 期望输出对照

| # | 步骤 | 接口 | 期望 | 实测观测值 |
| --- | --- | --- | --- | --- |
| 1 | 依赖就绪 | `GET /health/ready` | 200 `{"status":"ready"}` | `{"status":"ready"}` |
| 2 | 建项目 | `POST /api/projects` | 201，两个 scope id 非空 | `project:project-…` / `learner:project-…` |
| 3 | 导入资料 | `POST /api/projects/{id}/imports` | 200，assets/chunks>0，failed=0 | `assets=14 chunks=58 failed=0 skipped=0 restricted=1`（chunks 随正文长度浮动） |
| 4 | 受限资料可见性 | `POST /api/projects/{id}/search` | 检索不返回 restricted 正文 | `credential_scan_demo.md` 命中 `api_secret`，正文查不到 |
| 5 | 混合检索 | `POST /api/projects/{id}/search` | hits>0，每条带 citation + excerpt + locator | 5 hits，`[code] evaluate.py … {"line_start":1,"line_end":38}` |
| 6 | 「如何复现 baseline」 | 同上 | 能同时看到代码/配置/指标 | 命中 `train.py` / `baseline.yaml` / `metrics_baseline.md` / `reproduction_log.md` |
| 7 | 交接草案 | `POST /api/projects/{id}/handoff/prepare` | 200，返回 draft_id，`persisted=false` | `handoff-draft-…`，`persisted: false` |
| 8 | 确认交接 | `POST /api/projects/{id}/handoff/confirm` | 200，返回 `handoff_ref`（family/artifact_id/revision） | `{"family":"handoff","artifact_id":"handoff-…","revision":1}` |
| 9 | 读回交接 | `GET /api/projects/{id}/handoff` | 200，content 与确认时一致 | `objective/disposition/state/next_action` 一致 |
| 10 | 回执校验 | `POST /api/projects/{id}/handoff/acknowledge` | accepted 必须三项 checks 全 confirmed，否则 422 | 合规 200 `status=accepted`；缺 checks 422 `validation_failed` |
| 11 | 记录 Outcome | `POST /api/projects/{id}/handoff/outcome` | 200 | `{"status":"recorded","outcome_source_id":"source-…"}` |
| 12 | 新人测评 | `POST /api/projects/{id}/assessments` | 201，回显推导字段 | `needs_foundations=false paper_depth=45 environment_minutes=45` |
| 13 | 生成路径 | `POST /api/projects/{id}/plans` | 201，`status=active`，7 个任务，每个都有 citation | `version=1 status=active tasks=7`，全部带引用 |
| 14 | 阶段顺序 | `GET /api/plans/{plan_id}` | 只有 stage 0 是 `todo`，其余 `locked` | `[("0","todo"), ("1"…("6","locked"))]` |
| 15 | 推进任务 | `PATCH /api/tasks/{id}` | `todo→in_progress→done`，done 必须带声明的证据键 | 200；缺 `summary` 或 `evidence={}` 均 409 `task_update_rejected reason=missing_evidence missing_count=1`；`todo→locked` 409 `illegal_transition` |
| 16 | 后继解锁 | `GET /api/plans/{id}` | 完成 stage 0 后 stage 1 变 `todo` | `{0:done, 1:todo, 2…6:locked}` |
| 17 | 跨会话恢复 | `GET /api/projects/{id}/learning-context` | 200，阶段/下一任务/已完成/阻塞 + 记忆来源 | `current_stage=1 progress={done:1,total:7} memory_source=powercontext` |
| 18 | 资料问答 | `POST /api/projects/{id}/questions` | 答案必须带 citation；无证据时明确拒答 | 引用 4 条；空项目返回「无法从项目资料确认。」且 citations 为空 |
| 19 | 重规划 | `POST /api/projects/{id}/plans/replan` | 新 version，已完成任务带 `source_task_id` 保留 | `version=2`，stage 0 以 done 继承 |
| 20 | 反向交接 | `POST /api/projects/{id}/handoff/draft` | 200，草稿 disposition 由进度推得 | `disposition=continuable` |

## 规格 24.3 的逐条验收

- **检索结果包含可打开的 Citation**：`asset_id / kind / title / uri / locator / excerpt` 六项齐全，
  `uri` 是导入时解析出的真实文件路径。
- **学习路径覆盖规范/理论/环境/代码/实验/后续**：七阶段固定为
  `lab_orientation → paper_foundations → environment_data → code_tour → baseline_reproduction →
  continued_work → reverse_handoff`，阶段 4 缺 code/config/experiment 任一类型就只出草稿并记
  `plan_not_activated reason=insufficient_cited_evidence`。
- **服务器相关任务优先于实验任务**：stage 0 规范 + stage 2 环境都在 stage 4 实验之前，
  前置链由 `enforce_stage_order` 与 `environment_before_experiments` 拦住。
- **实验复现任务同时引用代码、配置和指标**：stage 4 的 `required_evidence` 就是这三种类型，
  校验器还会单独检查引用的 kind 与资产一致、状态为 active。
- **新会话返回当前阶段/最近完成/新问题/下一任务**：`learning-context` 的
  `current_stage / completed / blockers / next_task` 四项，且全部来自 seekdb 事实。
- **seekdb 里有真实检索记录**：`uv run --directory backend pytest` 里那 4 个 skip 项就是这条，
  需要 `LABRELAY_SEEKDB_TEST=1` 连真实 seekdb 跑，见 `../operations/seekdb.md`。
- **PowerContext 里有真实 Memory/Handoff/召回**：**这条尚未验证**，需要真实服务端，
  见 `../operations/powercontext.md` 的验证状态说明。
- **重规划不删除已提交证据**：完成记录通过 `source_task_id` 映射到新任务 id，
  事件仍按源任务 id 查询，冒烟第 19 步会打印继承下来的 done 任务。

## 前端手工核对清单

浏览器里按顺序做，任何一步卡住都是真问题：

1. 建项目 → 出现「2. 导入资料目录」；检索/交接/测评此时都禁用。
2. 导入 → 「已导入 14 项资料，58 个片段；失败 0，跳过 0，受限 1。」
3. 检索 → 命中条目显示 `[kind] 标题`、摘录正文、路径与 locator。
4. 学长交接 → 「生成交接草案」后「确认并提交 Handoff」才可点；确认后显示 `handoff_ref`。
5. 测评 → 「提交测评并生成路径」，出现「已生成学习路径 v1（状态：active）」。
6. 学习路径看板 → 只有阶段 0 标「可开始」。
7. 任务详情 → 「开始」变「进行中」；填完成总结后「提交完成」→ 看板里阶段 0 变「已完成」、
   **阶段 1 同时变「可开始」**（这一步曾经过不去，是重读整份计划才修好的）。
8. 学习进度侧栏 → 阶段与 `完成 n/7` 同步更新，`记忆来源` 显示实际来源。
9. 围绕资料提问 → 答案带引用；离线确定性模型的答复会显式标注「离线确定性模型，未做推理」。
