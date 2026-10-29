# PowerContext 在 LabRelay 里负责什么

## 职责边界

```text
PowerContext = 决策、Work Contract、Handoff（Prepared/Committed）、Outcome 与跨会话记忆
```

项目事实（资料、片段、引用、计划、任务、事件）一律在 seekdb，PowerContext 里不放副本。
PowerContext 承担的是「为什么这么决定、交接内容是什么、上一会话说过什么」这类需要
可追溯、可续接的历史。

## 用到的 Scope 模型

每个项目两个 Scope：

| Scope | id 形态 | 装什么 |
| --- | --- | --- |
| project scope | `{project.title}:project:{project.id}` 建出来的 scope_id | 交接决策、Work Contract、Handoff、Outcome |
| learner scope | `{project.title}:learner:{project.id}` 建出来的 scope_id | 新人进度、卡点、路径版本变化 |

Scope 通过 `CreateScopeRequest(idempotency_key="labrelay:project:{id}")` 创建，
重复创建同一个项目不会开出两套 Scope。

## 适配器覆盖的操作

`backend/src/labrelay/context/powercontext.py` 是对 `powercontext[client]>=1.4.1` 的薄封装，
应用层看不到生成模型类型。用到的客户端调用：

| LabRelay 动作 | PowerContext 调用 |
| --- | --- |
| 建 Scope | `create_scope` |
| 记一条记忆 | `remember_memory` |
| 取有界历史上下文 | `prepare_context` |
| 提交交接证据 Source | `create_source` |
| 建 Work Contract | `create_work_contract` |
| 生成当前工作交接 | `handoff_current_work` |
| 固化交接版本 | `commit_handoff` |
| 续接/读取交接 | `continue_handoff` |
| 接手人回执 | `acknowledge_handoff` |
| 记录任务 Outcome | `record_task_outcome` |
| 存活探测 | `get_liveness` |

故障映射：`TransportError` / `UnavailableResponseError` / 5xx / 401 / 403 一律转成
`PowerContextUnavailableError`，API 层回 503 `dependency_unavailable`。
其余 4xx 原样抛出——那是请求本身错了，不该谎报成服务不可用。

## 交接链路的三条硬规则

1. **prepare 只入库，不产生 PowerContext 版本。** 草案存在 seekdb 的 `handoff_drafts`，
   只有 `/handoff/confirm` 才会建 Source、Work Contract 并 commit 出一个 revision。
   这一条保证交接版本不会在用户没确认的情况下出现。
2. **accepted 回执必须三项 receiver checks 全 `confirmed`。** PowerContext 服务端会拒绝
   `live_state` / `capability` / `authorization` 未全部确认的 accepted 回执。LabRelay 在请求模型里
   先校验，客户端错误提前变成 422，而不是打成一个 500。非 accepted 回执必须带 message。
3. **Outcome 不等于经验被批准。** 记录 Outcome 只登记为 `untrusted_observation`，
   不会把任务结果升格成经确认的项目知识。

## LabRelay 引用与 PowerContext 引用不是一回事

LabRelay 的 Citation 指向 seekdb 里的 asset/chunk（`asset_id` + locator），
PowerContext 的引用是 `ArtifactReference` / `SourceReference`。
交接里两侧语义不同，实现上的处理是：Work Contract 的 facts 用 `basis = declared` + 空 evidence，
真实的 LabRelay 引用随前一步创建的 Source 正文一起带过去，不伪装成 PowerContext 的 artifact ref。

## 启动方式（未实跑）

后端依赖装的是 `powercontext[client]`，不含服务端 CLI（`powercontext` 命令需要 `typer`，
本地实测报 `ModuleNotFoundError: No module named 'typer'`）。若要起服务，需要额外安装 server 侧依赖：

```bash
uv sync --directory backend --extra dev --extra server
uv run --directory backend powercontext server --help
PYTHONPATH="$(pwd)/backend/src" POWERCONTEXT_URL=http://127.0.0.1:8000 \
  POWERCONTEXT_TOKEN=<token> \
  uv run --directory backend uvicorn labrelay.api.main:create_default_app \
  --factory --host 127.0.0.1 --port 8010
```

> **验证状态：PowerContext 服务端从未实跑过。**
>
> 上面的命令没有在本机执行成功过，因此 `POWERCONTEXT_TOKEN` 的认证行为、
> 服务端对 Work Contract / Handoff payload 的实际校验强度、以及 Scope 幂等键的落库形态，
> 都是**按 1.4.1 客户端生成的模型和内置 schema 推导出来的，不是跑出来的**。
>
> 已验证的部分只有：
> - 客户端模型的字段约束（`PreparedHandoff`、`HandoffContent`、`ArtifactReference`、
>   `SourceReference`、`CreateSourceRequest` 的 json schema 已逐条读过并写进测试）；
> - 适配器在离线替换实现下能走通完整交接链路（内存实现严格复用同一套 payload 校验）；
> - 严格回执规则在 422 层被拦住。
>
> 第一次真正连服务端时最可能出问题的地方，就是 `docs/verification/troubleshooting.md`
> 里标了「未实跑」的那几条。

## 不可用时的降级

PowerContext 挂了不影响项目事实和进度：

| 场景 | 行为 |
| --- | --- |
| 检索 / 计划生成 / 任务读写 | 正常，200 |
| 学习状态恢复 `learning-context` | 正常 200，记忆文本回退本地摘要，`memory_source` 标明来源 |
| 交接确认 / 回执 / Outcome | 503 `dependency_unavailable`，不会写半个版本进 seekdb |
| `/health/ready` | 503 |
